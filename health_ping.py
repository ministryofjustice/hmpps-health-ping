#!/usr/bin/env python
"""Health ping - fetches all /health and /info endpoints and stores the results in Redis"""

from datetime import datetime, timezone
import psutil
import os
import threading
import requests
import json
from time import sleep
import redis
import http.server
import socketserver
from hmpps import Slack, GithubSession, ServiceCatalogue
from hmpps.services.job_log_handling import (
  log_debug,
  log_error,
  log_info,
  log_critical,
  log_warning,
  job,
)

max_threads = int(os.getenv('MAX_THREADS', '200'))
refresh_interval = int(os.getenv('REFRESH_INTERVAL', '60'))
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()

# A list of tuples of environment field names mapped to redis field names
endpoints_list = [('health_path', 'health'), ('info_path', 'info')]


class Services:
  def __init__(self, sc_params, gh_params, slack_params, redis_params):
    self.slack = Slack(slack_params)
    self.sc = ServiceCatalogue(sc_params)
    self.gh = GithubSession(gh_params)
    self.redis_max_stream_length = redis_params['redis_max_stream_length']
    redis_params.pop('redis_max_stream_length')
    self.redis = self.connect_to_redis(redis_params)

  def connect_to_redis(self, redis_params):
    # Test connection to redis
    try:
      redis_params['ssl_cert_reqs'] = None
      redis_params['decode_responses'] = True

      redis_session = redis.Redis(**redis_params)
      redis_session.ping()
      log_info('Successfully connected to redis.')
      # Create root objects for latest if they don't exist
      for root_object in ['latest:health', 'latest:info', 'latest:versions']:
        if not redis_session.exists(root_object):
          log_debug(f'{root_object} does not exist - initialising it')
          redis_session.json().set(root_object, '$', {})

      log_debug('Redis is ready to go.')
    except Exception as e:
      log_critical('Unable to connect to redis.')
      self.slack.alert(f'*{job.name} failed*: Unable to connect to redis.')
      self.sc.update_scheduled_job('Failed')
      raise SystemExit(e)
    return redis_session


def get_build_image_tag(output):
  version = ''
  version_locations = (
    "output['build']['version']",  # all apps should have version here
    "output['components']['healthInfo']['details']['version']",  # Java/Kotlin springboot apps
    "output['build']['buildNumber']",  # Node/Typescript apps
  )
  for loc in version_locations:
    try:
      version = eval(loc)
      log_debug(f'version found in {loc}: {version}')
    except KeyError:  # no match to the key
      continue
    except AttributeError:  # there's NoneType going on
      continue
    except TypeError:  # empty string
      continue
  return version


def git_compare_commits(github_repo, from_sha, to_sha, services):
  comparison = []
  try:
    repo = services.gh.get_org_repo(github_repo)
    results = repo.compare(from_sha, to_sha)
    for commit in results.commits:
      comparison.append(
        {
          'sha': commit.sha,
          'html_url': commit.html_url,
          'message': commit.commit.message,
        }
      )
  except Exception as e:
    log_error(
      f'Error retreiving commits for repo: {github_repo} between {from_sha} and {to_sha} : {e}'
    )
  return comparison


def get_http_endpoint(endpoint):
  stream_data = {}
  output = {}
  try:
    log_debug(f'making call to: {endpoint}')
    # Override default User-Agent other gets blocked by mod security.
    headers = {'User-Agent': 'hmpps-health-ping'}
    r = requests.get(endpoint, headers=headers, timeout=10)
    output = r.json()
    log_debug(f'Response received: {output}')
    try:
      stream_data.update({'json': str(json.dumps(output))})
      # log_info(app_version)
    except Exception as e:
      log_error(f'{endpoint}: Unable to update stream_data with json - exception: {e}')

    stream_data.update({'http_s': r.status_code})
    log_info(f'http response: {r.status_code}: {endpoint}')

  except requests.exceptions.RequestException as e:
    # Set status code to 0 for failed connections
    stream_data.update({'http_s': 0})
    # Log error in stream for easier diagnosis of problems
    stream_data.update({'error': str(e)})
    log_error(f'Failed to get data from {endpoint} - exception: {e}')
  except Exception as e:
    log_error(f'Failed to parse response from {endpoint} - exception: {e}')
  return output, stream_data


def get_active_agencies(output, endpoint_type):
  active_agencies_dict = {}
  try:
    if ('activeAgencies' in output) and (endpoint_type == 'info'):
      active_agencies = output['activeAgencies']

      log_info(f'SC active_agencies: {env["active_agencies"]}')
      log_info(f'Existing active_agencies: {active_agencies}')

      # if current active_agencies is empty/None set to empty list to enable comparison.
      env_active_agencies = []
      if env['active_agencies'] is not None:
        env_active_agencies = env['active_agencies']
      # Test if active_agencies has changed, and update SC if so.
      if sorted(active_agencies) != sorted(env_active_agencies):
        active_agencies_dict = {'active_agencies': active_agencies}
  except (KeyError, TypeError):
    pass
  except Exception as e:
    log_error(f'failed to process active_agencies: {e}')
  return active_agencies_dict


def update_app_version(
  app_version, update_version_history, c_name, e_name, github_repo, services
):
  log_debug(f'Starting update_app_version for {c_name}-{e_name}')
  version_key = f'version:{c_name}:{e_name}'
  version_data = {'v': app_version, 'dateAdded': datetime.now(timezone.utc).isoformat()}

  # Getting into the redis update bit
  try:
    with services.redis.lock(
      f'{c_name}_{e_name}', timeout=5, blocking=True, blocking_timeout=5
    ) as lock:
      log_debug(f'Got lock: {lock.locked()}, {lock.name}')

    # Only update the version history if it's changed
    if update_version_history:
      # Find the previous version different to current app version
      # Due to some duplicate entries we need to search the stream.
      versions_history = services.redis.xrevrange(
        version_key, max='+', min='-', count=10
      )

      if versions_history != []:
        previous_deployed_version_key = False
        for i, v in enumerate(versions_history):
          if v[1]['v'] != app_version:
            previous_deployed_version_key = i
            break

        latest_version_from_redis = versions_history[0][1]['v']
        # Only add latest version to redis stream if it has changed since last entry.
        if latest_version_from_redis != app_version:
          app_version_sha = app_version.split('.')[-1]
          # If we have a previously deployed version - get the git commits since.
          if isinstance(previous_deployed_version_key, int):
            previous_deployed_version = versions_history[previous_deployed_version_key][
              1
            ]['v']
            previous_deployed_version_sha = previous_deployed_version.split('.')[-1]
            commits = git_compare_commits(
              github_repo, previous_deployed_version_sha, app_version_sha, services
            )
            log_info(f'Fetching commits for build: {app_version}')
            version_data.update({'git_compare': json.dumps(commits)})
          services.redis.xadd(version_key, version_data, maxlen=200, approximate=False)
          log_info(
            f'Updated redis stream with new version. {version_key} = {version_data}'
          )
      else:
        # Must be first time entry to version redis stream
        services.redis.xadd(version_key, version_data, maxlen=200, approximate=False)
        log_debug(f'Adding first entry to version: {version_key} = {version_data}')

    # Always update the latest version key
    services.redis.json().set('latest:versions', f'$.{version_key}', version_data)
    log_info(
      f'Updated latest:versions redis key with latest version. {version_key} = {version_data}'
    )
  except Exception as e:
    log_error(f'Failed to update redis versions - {e}')

  log_debug(f'Completed update_app_version for {c_name}-{e_name}')


def process_env(c_name, component, env_id, env, endpoints_list, services):
  log_info(f'Processing {env.get("name")}')
  log_debug(f'Memory usage: {process.memory_info().rss / 1024**2} MB')

  # variables to store just once for all attributes
  app_version = None
  update_version_history = False

  # main loop for the endpoint types
  for endpoint_tuple in endpoints_list:
    if endpoint_uri := env.get(endpoint_tuple[0]):
      endpoint = f'{env["url"]}{endpoint_uri}'
      endpoint_type = endpoint_tuple[1]
      log_debug(f'endpoint: {endpoint}')
      # Redis key to use for stream
      e_name = env['name']
      log_debug(f'environment name e_name={e_name}')
      stream_key = f'{endpoint_type}:{c_name}:{e_name}'
      log_debug(f'stream_key={stream_key}')
      stream_data = {}
      stream_data.update({'url': endpoint})
      stream_data.update({'dateAdded': datetime.now(timezone.utc).isoformat()})

      # Get component id
      c_id = component.get('documentId', '')

      # make the call to the endpoint
      output, stream_updated_data = get_http_endpoint(endpoint)
      if stream_updated_data:
        stream_data.update(stream_updated_data)

      # Try to get app version (HEAT-567 - get app version from build image tag on health or info)
      env_data = {}
      update_sc = False
      if app_version := get_build_image_tag(output):
        log_debug(f'Found app version: {c_name}:{e_name}:{app_version}')
        image_tag = []
        image_tag = env.get('build_image_tag', '')
        log_debug((f'existing build_image_tag: {image_tag}'))
        if app_version and app_version != image_tag:
          env_data.update({'build_image_tag': app_version})
          update_sc = True
          log_info(
            f'Updating build_image_tag for component  {c_id} {c_name} - Environment {env_id} {e_name}{env_data}'
          )
          update_version_history = True
        else:
          log_debug(
            f'No change in build_image_tag for component  {c_id} {c_name} - Environment {env_id} {e_name}'
          )
        # leave the redis processing of the app version to the end of the loop
      else:
        log_info(
          f'No app_version data in {endpoint_tuple[1]} endpoint for {env.get("name")}'
        )

      # Try to get active agencies
      if active_agencies := get_active_agencies(output, endpoint_type):
        env_data.update(active_agencies)
        update_sc = True

      if update_sc:
        services.sc.update(services.sc.environments, env_id, env_data)

      # This is the bit where the redis stream gets updated - needs to be outdented
      log_debug(f'Updating redis stream {stream_key} with {stream_data}')
      try:
        services.redis.xadd(
          stream_key,
          stream_data,
          maxlen=services.redis_max_stream_length,
          approximate=False,
        )
        services.redis.json().set(
          f'latest:{endpoint_type}', f'$.{stream_key}', stream_data
        )
        log_debug(f'Redis stream updated - {stream_key}: {stream_data}')
      except Exception as e:
        log_error(f'Unable to add data to redis stream. {e}')

        log_debug(f'Completed process_env for {env.get("name")}:{endpoint_tuple[1]}')

    else:
      log_warning(f'No endpoint URI found for {endpoint_tuple[1]}')
  # loop ends here

  # Now update the redis DB once for any of the attributes if there's a change
  log_debug(
    f'checking app_version and update_version_history for {c_name}-{env.get("name")}'
  )
  if app_version:
    log_debug(
      f'app_version:({app_version}) and update_version_history is {update_version_history}'
    )
    github_repo = component.get('github_repo', '')
    update_app_version(
      app_version, update_version_history, c_name, e_name, github_repo, services
    )
  else:
    log_debug('no app version')


class HealthHttpRequestHandler(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    self.send_response(200)
    self.send_header('Content-type', 'text/plain')
    self.end_headers()
    self.wfile.write(bytes('UP', 'utf8'))
    return


def startHttpServer():
  handler_object = HealthHttpRequestHandler
  with socketserver.TCPServer(('', 3000), handler_object) as httpd:
    httpd.serve_forever()


if __name__ == '__main__':
  process = psutil.Process(os.getpid())
  main_threads = list()
  http_thread = list()
  # Start health endpoint.
  httpHealth = threading.Thread(target=startHttpServer, daemon=True)
  http_thread.append(httpHealth)
  httpHealth.start()

  job.name = 'hmpps-health-ping'

  # Set up slack notification params
  slack_params = {
    'token': os.getenv('SLACK_BOT_TOKEN'),
    'notify_channel': os.getenv('SLACK_NOTIFY_CHANNEL', ''),
    'alert_channel': os.getenv('SLACK_ALERT_CHANNEL', ''),
  }

  # Github parameters
  gh_params = {
    'app_id': int(os.getenv('GITHUB_APP_ID', '0')),
    'app_installation_id': int(os.getenv('GITHUB_APP_INSTALLATION_ID', '0')),
    'app_private_key': os.getenv('GITHUB_APP_PRIVATE_KEY', ''),
  }

  sc_params = {
    'url': os.getenv('SERVICE_CATALOGUE_API_ENDPOINT', ''),
    'key': os.getenv('SERVICE_CATALOGUE_API_KEY', ''),
    'filter': os.getenv('SC_FILTER', ''),
  }

  redis_params = {
    'host': os.getenv('REDIS_ENDPOINT'),
    'port': os.getenv('REDIS_PORT'),
    'ssl': os.getenv('REDIS_TLS_ENABLED', 'False').lower()
    in (
      'true',
      '1',
      't',
    ),
    'password': os.getenv('REDIS_TOKEN', ''),
    'redis_max_stream_length': int(os.getenv('REDIS_MAX_STREAM_LENGTH', '360')),
  }

  services = Services(sc_params, gh_params, slack_params, redis_params)

  while True:
    log_info(
      f'Starting a new run. Service Catalogue endpoint: {sc_params.get("url")}. Current memory usage: {process.memory_info().rss / 1024**2} MB'
    )
    components = services.sc.get_all_records(services.sc.components_get)
    for component in components:
      c_name = component.get('name')
      for env in component.get('envs', []):
        env_id = env.get('documentId', '')
        if env.get('url') and env.get('monitor'):
          # moving the endpoint_tuple loop inside the process_env
          # to avoid duplication of build_image_tag if it's present in both health and info
          thread = threading.Thread(
            target=process_env,
            args=(c_name, component, env_id, env, endpoints_list, services),
            daemon=True,
          )
          main_threads.append(thread)
          # Apply limit on total active threads, avoid github secondary API rate limit
          while threading.active_count() > (max_threads - 1):
            log_info(
              f'Active Threads={threading.active_count()}, Max Threads={max_threads} - backing off for a few seconds'
            )
            sleep(3)
          thread.start()
          log_info(
            f'Started thread for {env.get("name")} (active threads: {threading.active_count()})'
          )
        else:
          continue
      log_debug(f'Active threads: {threading.active_count()}')

    # Allow the threads to finish before sleeping
    for thread in main_threads:
      thread.join()
    log_info(
      f'Completed all threads. Sleeping for {refresh_interval} seconds. Current memory usage: {process.memory_info().rss / 1024**2} MB.'
    )
    # Even if job had errors , error will be recorded in SC and job will be marked successful
    # as few services are expected to fail.
    if job.error_messages:
      services.sc.update_scheduled_job('Errors')
      log_info(f'{job.name} job completed with errors.')
    else:
      services.sc.update_scheduled_job('Succeeded')
      log_info(f'{job.name} job completed successfully.')

    # Clear the main threads list and error messages for the next run
    main_threads.clear()
    job.error_messages.clear()
    sleep(refresh_interval)
