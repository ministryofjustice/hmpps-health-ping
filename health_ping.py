#!/usr/bin/env python
'''Health ping - fetches all /health and /info endpoints and stores the results in Redis'''
from datetime import datetime, timezone
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

sc_api_endpoint = os.getenv("SERVICE_CATALOGUE_API_ENDPOINT")
sc_api_token = os.getenv("SERVICE_CATALOGUE_API_KEY")
redis_host = os.getenv("REDIS_ENDPOINT")
redis_port = os.getenv("REDIS_PORT")
redis_tls_enabled = os.getenv("REDIS_TLS_ENABLED", 'False').lower() in ('true', '1', 't')
redis_token = os.getenv("REDIS_TOKEN", "")
redis_max_stream_length = int(os.getenv("REDIS_MAX_STREAM_LENGTH", "360"))
refresh_interval = int(os.getenv("REFRESH_INTERVAL","60"))
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
GITHUB_APP_ID = int(os.getenv('GITHUB_APP_ID'))
GITHUB_APP_INSTALLATION_ID = int(os.getenv('GITHUB_APP_INSTALLATION_ID'))
GITHUB_APP_PRIVATE_KEY = os.getenv('GITHUB_APP_PRIVATE_KEY')

# limit results for testing/dev
# See strapi filter syntax https://docs.strapi.io/dev-docs/api/rest/filters-locale-publication
# Example filter string = '&filters[name][$contains]=example'
# sc_api_filter = '&filters[name][$contains]=book-a-prison-visit-staff-ui'
sc_api_filter = os.getenv("SERVICE_CATALOGUE_FILTER", '')

# Example Sort filter
#SC_SORT='&sort=updatedAt:asc'


def update_sc_component(c_id, data):
  try:
    log.debug(f"Data to POST to strapi {data}")
    x = requests.put(f"{sc_api_endpoint}/v1/components/{c_id}", headers=sc_api_headers, json = {"data": data}, timeout=10)
    if x.status_code == 200:
      log.info(f"Successfully updated component id {c_id}: {x.status_code}")
    else:
      log.info(f"Received non-200 response from service catalogue for component id {c_id}: {x.status_code} {x.content}")
  except Exception as e:
    log.error(f"Error updating component in the SC: {e}")

def git_compare_commits(github_repo, from_sha, to_sha):
  repo = gh.get_repo(f'ministryofjustice/{github_repo}')
  results = repo.compare(from_sha, to_sha)
  comparison = []
  for commit in results.commits:
    comparison.append({'sha': commit.sha, 'html_url': commit.html_url, 'message': commit.commit.message })
  return comparison

def update_app_version(app_version, c_name, e_name, github_repo):
  version_key = f'version:{c_name}:{e_name}'
  version_data = {'v': app_version, 'dateAdded': datetime.now(timezone.utc).isoformat()}
  try:
    with redis.lock(f"{c_name}_{e_name}", timeout=5, blocking=True, blocking_timeout=5) as lock:
      log.debug(f"Got lock: {lock.locked()}, {lock.name}")

      # Find the previous version different to current app version
      # Due to some duplicate entries we need to search the stream.
      versions_history = redis.xrevrange(version_key, max='+', min='-', count=10)

      if versions_history != []:
        previous_deployed_version_key = False
        for i,v in enumerate(versions_history):
          if v[1]['v'] != app_version:
            previous_deployed_version_key = i
            break

        latest_version_from_redis = versions_history[0][1]['v']
        # Only add latest version to redis stream if it has changed since last entry.
        if latest_version_from_redis != app_version:
          app_version_sha = app_version.split('.')[-1]
          # If we have a previously deployed version - get the git commits since.
          if isinstance(previous_deployed_version_key, int):
            previous_deployed_version = versions_history[previous_deployed_version_key][1]['v']
            previous_deployed_version_sha = previous_deployed_version.split('.')[-1]
            commits = git_compare_commits(github_repo, previous_deployed_version_sha, app_version_sha)
            log.info(f"Fetching commits for build: {app_version}")
            version_data.update({'git_compare': json.dumps(commits)})
          redis.xadd(version_key, version_data, maxlen=200, approximate=False)
          log.info(f'Updating redis stream with new version. {version_key} = {version_data}')

      else:
        # Must be first time entry to version redis stream
        redis.xadd(version_key, version_data, maxlen=200, approximate=False)
        log.debug(f"First version entry = {version_key}:{version_data}")
        return

    # Always update the latest version key
    redis.json().set('latest:versions', f'$.{version_key}', version_data)
    log.info(f'Updating redis key with latest version. {version_key} = {version_data}')
    if (endpoint_type == 'info'):
      env_data = []
      update_sc = False
      for e in component["attributes"]["environments"]:
        if e_name == e["name"]:
          if e["build_image_tag"] is None:
            e["build_image_tag"] = []
          if app_version != e["build_image_tag"]:
            env_data.append({"id": e["id"], "build_image_tag": app_version })
            update_sc = True
        else:
          env_data.append({"id": e["id"]})
      if update_sc:
        data = {"environments": env_data}
        update_sc_component(c_id, data)

  except Exception as e:
    log.error(e)

def process_env(c_name, e_name, endpoint, endpoint_type, component):
  output = {}
  # Redis key to use for stream
  stream_key = f'{endpoint_type}:{c_name}:{e_name}'
  stream_data = {}
  stream_data.update({'url': endpoint})
  stream_data.update({'dateAdded': datetime.now(timezone.utc).isoformat()})

  try:
    # Override default User-Agent other gets blocked by mod security.
    headers = {'User-Agent': 'hmpps-health-ping'}
    r = requests.get(endpoint, headers=headers, timeout=10)

    try:
      output = r.json()
      stream_data.update({'json': str(json.dumps(output))})
      #log.info(app_version)
    except Exception as e:
      log.error(f"{endpoint}: Unable to read json")
      log.error(e)

    stream_data.update({'http_s': r.status_code})
    log.info(f"{r.status_code}: {endpoint}")

  except requests.exceptions.RequestException as e:
    # Set status code to 0 for failed connections
    stream_data.update({'http_s': 0})
    # Log error in stream for easier diagnosis of problems
    stream_data.update({'error': str(e)})
    log.error(e)


  # Try to get app version.
  try:
    version_locations = (
      "output['build']['version']", # all apps should have version here
      "output['components']['healthInfo']['details']['version']", # Java/Kotlin springboot apps
      "output['build']['buildNumber']" # Node/Typscript apps
    )
    for loc in version_locations:
      try:
        app_version = eval(loc)
        log.debug(f"Found app version: {c_name}:{e_name}:{app_version}")
        github_repo = component["attributes"]["github_repo"]
        update_app_version(app_version, c_name, e_name, github_repo)
        break
      except (KeyError, TypeError):
        pass
      except Exception as e:
        log.error(f"{endpoint}: {type(e)} {e}")

  except Exception as e:
    log.error(e)

  # Try to get active agencies
  try:
    if ('activeAgencies' in output) and (endpoint_type == 'info'):
      active_agencies = output['activeAgencies']

      # Need to add all other env IDs to the data payload otherwise strapi will delete them.
      env_data = []
      update_sc = False
      for e in component["attributes"]["environments"]:
        # Work on current environment
        if e_name == e["name"]:
          # Existing active_agencies from the SC
          log.info(f"SC active_agencies: {e['active_agencies']}")
          log.info(f"Existing active_agencies: {active_agencies}")

          # if current active_agencies is empty/None set to empty list to enable comparison.
          if e["active_agencies"] is None:
            e["active_agencies"] = []
          # Test if active_agencies has changed, and update SC if so.
          if sorted(active_agencies) != sorted(e["active_agencies"]):
            env_data.append({"id": e["id"], "active_agencies": active_agencies })
            update_sc = True
        else:
          # Add rest of envs for strapi update.
          env_data.append({"id": e["id"]})

      if update_sc:
        data = {"environments": env_data}
        update_sc_component(c_id, data)
  except (KeyError, TypeError):
    pass
  except Exception as e:
    log.error(e)

  try:
    redis.xadd(stream_key, stream_data, maxlen=redis_max_stream_length, approximate=False)
    redis.json().set(f'latest:{endpoint_type}', f'$.{stream_key}', stream_data)
    log.debug(f"{stream_key}: {stream_data}")
  except Exception as e:
    log.error(f"Unable to add data to redis stream. {e}")

class HealthHttpRequestHandler(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    self.send_response(200)
    self.send_header("Content-type", "text/plain")
    self.end_headers()
    self.wfile.write(bytes("UP", "utf8"))
    return

def startHttpServer():
  handler_object = HealthHttpRequestHandler
  with socketserver.TCPServer(("", 8080), handler_object) as httpd:
    httpd.serve_forever()

if __name__ == '__main__':
  logging.basicConfig(
      format='[%(asctime)s] %(levelname)s %(threadName)s %(message)s', level=log_level)
  log = logging.getLogger(__name__)

  threads = list()
  # Start health endpoint.
  httpHealth = threading.Thread(target=startHttpServer, daemon=True)
  threads.append(httpHealth)
  httpHealth.start()

  # Test connection to redis
  try:
    redis_connect_args = dict(
      host = redis_host,
      port = redis_port,
      ssl = redis_tls_enabled,
      ssl_cert_reqs = None,
      decode_responses = True
    )
    if redis_token:
      redis_connect_args.update(dict(password=redis_token))
    redis = redis.Redis(**redis_connect_args)
    redis.ping()
    log.info("Successfully connected to redis.")
    # Create root objects for latest if they don't exist
    if not redis.exists('latest:health'):
      redis.json().set(f'latest:health', '$', {})
    if not redis.exists('latest:info'):
      redis.json().set('latest:info', '$', {})
    if not redis.exists('latest:versions'):
      redis.json().set('latest:versions', '$', {})
  except Exception as e:
    log.critical("Unable to connect to redis.")
    raise SystemExit(e)

  sc_api_headers = {"Authorization": f"Bearer {sc_api_token}", "Content-Type":"application/json","Accept": "application/json"}

  # Test connection to Service Catalogue
  try:
    r = requests.head(f"{sc_api_endpoint}/_health", headers=sc_api_headers, timeout=20)
    log.info(f"Successfully connected to the Service Catalogue. {r.status_code}")
  except Exception as e:
    log.critical("Unable to connect to the Service Catalogue.")
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

  sc_endpoint = f"{sc_api_endpoint}/v1/components?populate=environments{sc_api_filter}"

  while True:
    log.info(sc_endpoint)
    try:
      r = requests.get(sc_endpoint, headers=sc_api_headers, timeout=20)
      log.debug(r)
      if r.status_code == 200:
        j_data = r.json()["data"]
      else:
        raise Exception(f"Received non-200 response from Service Catalogue: {r.status_code}")
    except Exception as e:
      log.error(f"Unable to connect to Service Catalogue API. {e}")

    for component in j_data:
      for env in component["attributes"]["environments"]:
        c_name = component["attributes"]["name"]
        e_name = env["name"]
        # Current component ID needed for strapi api call
        c_id = component["id"]
        if (env["url"]) and (env["monitor"] == True):
          if env["health_path"]:
            endpoint = f'{env["url"]}{env["health_path"]}'
            endpoint_type = "health"
            t_health = threading.Thread(target=process_env, args=(c_name, e_name, endpoint, endpoint_type, component), daemon=True)
            threads.append(t_health)
            t_health.start()
            log.info(f"Started thread for {c_name}:{endpoint_type}")
          if env["info_path"]:
            endpoint = f'{env["url"]}{env["info_path"]}'
            endpoint_type = "info"
            t_info = threading.Thread(target=process_env, args=(c_name, e_name, endpoint, endpoint_type, component), daemon=True)
            threads.append(t_info)
            t_info.start()
            log.info(f"Started thread for {c_name}:{endpoint_type}")
        else:
          continue

    log.debug(f"Active threads: {threading.active_count()}")
    sleep(refresh_interval)
