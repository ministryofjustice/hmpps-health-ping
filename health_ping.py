#!/usr/bin/env python
"""Health ping - fetches all /health and /info endpoints and stores the results in Redis"""

from datetime import datetime, timezone
import psutil
import os
import threading
import logging
import requests
import json
from base64 import b64decode
from time import sleep
import redis
import http.server
import socketserver
import github

max_threads = os.getenv('MAX_THREADS', 200)
sc_api_endpoint = os.getenv('SERVICE_CATALOGUE_API_ENDPOINT')
sc_api_token = os.getenv('SERVICE_CATALOGUE_API_KEY')
redis_host = os.getenv('REDIS_ENDPOINT')
redis_port = os.getenv('REDIS_PORT')
redis_tls_enabled = os.getenv('REDIS_TLS_ENABLED', 'False').lower() in (
  'true',
  '1',
  't',
)
redis_token = os.getenv('REDIS_TOKEN', '')
redis_max_stream_length = int(os.getenv('REDIS_MAX_STREAM_LENGTH', '360'))
refresh_interval = int(os.getenv('REFRESH_INTERVAL', '60'))
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
GITHUB_APP_ID = int(os.getenv('GITHUB_APP_ID'))
GITHUB_APP_INSTALLATION_ID = int(os.getenv('GITHUB_APP_INSTALLATION_ID'))
GITHUB_APP_PRIVATE_KEY = os.getenv('GITHUB_APP_PRIVATE_KEY')

# limit results for testing/dev
# See strapi filter syntax https://docs.strapi.io/dev-docs/api/rest/filters-locale-publication
# Example filter string = '&filters[name][$contains]=example'
# sc_api_filter = '&filters[name][$contains]=book-a-prison-visit-staff-ui'
sc_api_filter = os.getenv('SERVICE_CATALOGUE_FILTER', '')

# Example Sort filter
# SC_SORT='&sort=updatedAt:asc'

# A list of tuples of environment field names mapped to redis field names
endpoints_list = [('health_path', 'health'), ('info_path', 'info')]


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
      log.debug(f'version found in {loc}: {version}')
    except KeyError:  # no match to the key
      continue
    except AttributeError:  # there's NoneType going on
      continue
    except TypeError:  # empty string
      continue
  return version


def update_sc_environment(env_id, env_data):
  data = {'data': env_data}
  try:
    log.debug(f'Data to POST to strapi {data}')
    x = requests.put(
      f'{sc_api_endpoint}/v1/environments/{env_id}',
      headers=sc_api_headers,
      json=data,
      timeout=10,
    )
    if x.status_code == 200:
      log.info(f'Successfully updated environment id {env_id}: {x.status_code}')
    else:
      log.info(
        f'Received non-200 response from service catalogue for environment id {env_id}: {x.status_code} {x.content}'
      )
  except Exception as e:
    log.error(f'Error updating environment in the SC: {e}')


def git_compare_commits(github_repo, from_sha, to_sha):
  comparison = []
  try:
    repo = gh.get_repo(f'ministryofjustice/{github_repo}')
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
    log.error(
      f'Error retreiving commits for repo: {github_repo} between {from_sha} and {to_sha} : {e}'
    )
  return comparison


def get_http_endpoint(endpoint):
  stream_data = {}
  output = {}
  try:
    log.debug(f'making call to: {endpoint}')
    # Override default User-Agent other gets blocked by mod security.
    headers = {'User-Agent': 'hmpps-health-ping'}
    r = requests.get(endpoint, headers=headers, timeout=10)
    output = r.json()
    log.debug(f'Response received: {output}')
    try:
      stream_data.update({'json': str(json.dumps(output))})
      # log.info(app_version)
    except Exception as e:
      log.error(f'{endpoint}: Unable to update stream_data with json')
      log.error(e)

    stream_data.update({'http_s': r.status_code})
    log.info(f'http response: {r.status_code}: {endpoint}')

  except requests.exceptions.RequestException as e:
    # Set status code to 0 for failed connections
    stream_data.update({'http_s': 0})
    # Log error in stream for easier diagnosis of problems
    stream_data.update({'error': str(e)})
    log.error(f'Failed to get data from {endpoint} - exception: {e}')
  except Exception as e:
    log.error(f'Failed to parse response from {endpoint} - exception: {e}')
  return output, stream_data


def get_active_agencies(output, endpoint_type):
  active_agencies_dict = {}
  try:
    if ('activeAgencies' in output) and (endpoint_type == 'info'):
      active_agencies = output['activeAgencies']

      log.info(f'SC active_agencies: {env_attributes["active_agencies"]}')
      log.info(f'Existing active_agencies: {active_agencies}')

      # if current active_agencies is empty/None set to empty list to enable comparison.
      env_active_agencies = []
      if env_attributes['active_agencies'] is not None:
        env_active_agencies = env_attributes['active_agencies']
      # Test if active_agencies has changed, and update SC if so.
      if sorted(active_agencies) != sorted(env_active_agencies):
        active_agencies_dict = {'active_agencies': active_agencies}
  except (KeyError, TypeError):
    pass
  except Exception as e:
    log.error(f'failed to process active_agencies: {e}')
  return active_agencies_dict


def update_app_version(app_version, c_name, e_type, github_repo):
  log.debug(f'Starting update_app_version for {c_name}-{e_type}')
  version_key = f'version:{c_name}:{e_type}'
  version_data = {'v': app_version, 'dateAdded': datetime.now(timezone.utc).isoformat()}
  try:
    with redis.lock(
      f'{c_name}_{e_type}', timeout=5, blocking=True, blocking_timeout=5
    ) as lock:
      log.debug(f'Got lock: {lock.locked()}, {lock.name}')

      # Find the previous version different to current app version
      # Due to some duplicate entries we need to search the stream.
      versions_history = redis.xrevrange(version_key, max='+', min='-', count=10)

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
              github_repo, previous_deployed_version_sha, app_version_sha
            )
            log.info(f'Fetching commits for build: {app_version}')
            version_data.update({'git_compare': json.dumps(commits)})
          redis.xadd(version_key, version_data, maxlen=200, approximate=False)
          log.info(
            f'Updated redis stream with new version. {version_key} = {version_data}'
          )
      else:
        # Must be first time entry to version redis stream
        redis.xadd(version_key, version_data, maxlen=200, approximate=False)
        log.debug(f'Adding first entry to version: {version_key} = {version_data}')
        return

    # Always update the latest version key
    redis.json().set('latest:versions', f'$.{version_key}', version_data)
    log.info(f'Updated redis key with latest version. {version_key} = {version_data}')

  except Exception as e:
    log.error(f'Failed to update redis: {e}')
  log.debug(f'Completed update_app_version for {c_name}-{e_type}')


def process_env(c_name, component, env_id, env_attributes, endpoints_list):
  log.info(f'Processing {env_attributes.get("name")}')
  log.debug(f'Memory usage: {process.memory_info().rss / 1024**2} MB')

  # variables to store just once for all attributes
  app_version = None
  update_version_redis = False

  # main loop for the endpoint types
  for endpoint_tuple in endpoints_list:
    if endpoint_uri := env_attributes.get(endpoint_tuple[0]):
      endpoint = f'{env_attributes["url"]}{endpoint_uri}'
      endpoint_type = endpoint_tuple[1]
      log.debug(f'endpoint: {endpoint}')
      # Redis key to use for stream
      # New environment table uses 'component-name-envname' as the name
      # so if the environment name contains a '-'
      # only add the bit after the '-' section
      # (while dev portal uses the environment subtable within components)
      if '-' in env_attributes['name']:
        e_name = env_attributes['name'].split('-')[-1]
      else:
        e_name = env_attributes['name']
      log.debug(f'environment name e_name={e_name}')
      e_type = env_attributes['type']
      stream_key = f'{endpoint_type}:{c_name}:{e_name}'
      log.debug(f'stream_key={stream_key}')
      stream_data = {}
      stream_data.update({'url': endpoint})
      stream_data.update({'dateAdded': datetime.now(timezone.utc).isoformat()})

      # Get component id
      c_id = component['id']

      # make the call to the endpoint
      output, stream_updated_data = get_http_endpoint(endpoint)
      if stream_updated_data:
        stream_data.update(stream_updated_data)

      # Try to get app version (HEAT-567 - get app version from build image tag on health or info)
      env_data = {}
      update_sc = False
      if app_version := get_build_image_tag(output):
        log.debug(f'Found app version: {c_name}:{e_name}:{app_version}')
        image_tag = []
        image_tag = env_attributes['build_image_tag']
        log.debug((f'existing build_image_tag: {image_tag}'))
        if app_version and app_version != image_tag:
          env_data.update({'build_image_tag': app_version})
          update_sc = True
          log.info(
            f'Updating build_image_tag for component  {c_id} {c_name} - Environment {env_id} {e_name}{env_data}'
          )
          update_version_redis = True
        else:
          log.debug(
            f'No change in build_image_tag for component  {c_id} {c_name} - Environment {env_id} {e_name}'
          )
        # leave the redis processing of the app version to the end of the loop
      else:
        log.info(
          f'No app_version data in {endpoint_tuple[1]} endpoint for {env_attributes.get("name")}'
        )

      # Try to get active agencies
      if active_agencies := get_active_agencies(output, endpoint_type):
        env_data.update(active_agencies)
        update_sc = True

      if update_sc:
        update_sc_environment(env_id, env_data)

      # This is the bit where the redis stream gets updated - needs to be outdented
      log.debug(f'Updating redis stream {stream_key} with {stream_data}')
      try:
        redis.xadd(
          stream_key, stream_data, maxlen=redis_max_stream_length, approximate=False
        )
        redis.json().set(f'latest:{endpoint_type}', f'$.{stream_key}', stream_data)
        log.debug(f'Redis stream updated - {stream_key}: {stream_data}')
      except Exception as e:
        log.error(f'Unable to add data to redis stream. {e}')

        log.debug(
          f'Completed process_env for {env_attributes.get("name")}:{endpoint_tuple[1]}'
        )

    else:
      log.warning(f'No endpoint URI found for {endpoint_tuple[1]}')
  # loop ends here

  # Now update the redis DB once for any of the attributes if there's a change
  log.debug(
    f'checking app_version and update_version_redis for {c_name}-{env_attributes.get("name")}'
  )
  if app_version and update_version_redis:
    log.debug(
      f'app_version has been updated ({app_version}) and update_version_redis is true'
    )
    github_repo = component['attributes']['github_repo']
    update_app_version(app_version, c_name, e_type, github_repo)
  else:
    log.debug('no need to update version')


class HealthHttpRequestHandler(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    self.send_response(200)
    self.send_header('Content-type', 'text/plain')
    self.end_headers()
    self.wfile.write(bytes('UP', 'utf8'))
    return


def startHttpServer():
  handler_object = HealthHttpRequestHandler
  with socketserver.TCPServer(('', 8080), handler_object) as httpd:
    httpd.serve_forever()


if __name__ == '__main__':
  logging.basicConfig(
    format='[%(asctime)s] %(levelname)s %(threadName)s %(message)s', level=log_level
  )
  log = logging.getLogger(__name__)
  process = psutil.Process(os.getpid())
  main_threads = list()
  http_thread = list()
  # Start health endpoint.
  httpHealth = threading.Thread(target=startHttpServer, daemon=True)
  http_thread.append(httpHealth)
  httpHealth.start()

  # Test connection to redis
  try:
    redis_connect_args = dict(
      host=redis_host,
      port=redis_port,
      ssl=redis_tls_enabled,
      ssl_cert_reqs=None,
      decode_responses=True,
    )
    if redis_token:
      redis_connect_args.update(dict(password=redis_token))
    redis = redis.Redis(**redis_connect_args)
    redis.ping()
    log.info('Successfully connected to redis.')
    # Create root objects for latest if they don't exist
    if not redis.exists('latest:health'):
      redis.json().set('latest:health', '$', {})
    if not redis.exists('latest:info'):
      redis.json().set('latest:info', '$', {})
    if not redis.exists('latest:versions'):
      redis.json().set('latest:versions', '$', {})
  except Exception as e:
    log.critical('Unable to connect to redis.')
    raise SystemExit(e)

  sc_api_headers = {
    'Authorization': f'Bearer {sc_api_token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  }

  # Test connection to Service Catalogue
  try:
    r = requests.head(f'{sc_api_endpoint}/_health', headers=sc_api_headers, timeout=20)
    log.info(f'Successfully connected to the Service Catalogue. {r.status_code}')
  except Exception as e:
    log.critical('Unable to connect to the Service Catalogue.')
    raise SystemExit(e)

  # Test auth and connection to github
  try:
    private_key = b64decode(GITHUB_APP_PRIVATE_KEY).decode('ascii')
    auth = github.Auth.AppAuth(GITHUB_APP_ID, private_key).get_installation_auth(
      GITHUB_APP_INSTALLATION_ID
    )
    gh = github.Github(auth=auth, pool_size=50)

    rate_limit = gh.get_rate_limit()
    core_rate_limit = rate_limit.core
    log.info(f'Github API: {rate_limit}')
    # test fetching organisation name
    gh.get_organization('ministryofjustice')
  except Exception as e:
    log.critical('Unable to connect to the github API.')
    raise SystemExit(e) from e

  sc_endpoint = f'{sc_api_endpoint}/v1/components?populate=envs{sc_api_filter}'

  while True:
    log.info(
      f'Starting a new run. Service Catalogue endpoint: {sc_endpoint}. Current memory usage: {process.memory_info().rss / 1024**2} MB'
    )
    try:
      r = requests.get(sc_endpoint, headers=sc_api_headers, timeout=20)
      log.debug(r)
      if r.status_code == 200:
        j_data = r.json()['data']
      else:
        raise Exception(
          f'Received non-200 response from Service Catalogue: {r.status_code}'
        )
    except Exception as e:
      log.error(f'Unable to connect to Service Catalogue API. {e}')

    for component in j_data:
      for env in component['attributes']['envs']['data']:
        c_name = component['attributes']['name']
        env_attributes = env['attributes']
        env_id = env['id']
        if env_attributes.get('url') and env_attributes.get('monitor'):
          # moving the endpoint_tuple loop inside the process_env
          # to avoid duplication of build_image_tag if it's present in both health and info
          thread = threading.Thread(
            target=process_env,
            args=(c_name, component, env_id, env_attributes, endpoints_list),
            daemon=True,
          )
          main_threads.append(thread)
          # Apply limit on total active threads, avoid github secondary API rate limit
          while threading.active_count() > (max_threads - 1):
            log.info(
              f'Active Threads={threading.active_count()}, Max Threads={max_threads} - backing off for a few seconds'
            )
            sleep(3)
          thread.start()
          log.info(
            f'Started thread for {env_attributes.get("name")} (active threads: {threading.active_count()})'
          )
        else:
          continue
      log.debug(f'Active threads: {threading.active_count()}')

    # Allow the threads to finish before sleeping
    for thread in main_threads:
      thread.join()
    log.info(
      f'Completed all threads. Sleeping for {refresh_interval} seconds. Current memory usage: {process.memory_info().rss / 1024**2} MB.'
    )
    # Added clearing the main_threads list to release memory
    main_threads.clear()
    sleep(refresh_interval)
