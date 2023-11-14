#!/usr/bin/env python
'''Health ping - fetches all /health and /info endpoints and stores the results in Redis'''
import os
import sys
import threading
import logging
import requests
import json
from datetime import datetime
from time import sleep
import redis
import http.server
import socketserver

sc_api_endpoint = os.getenv("SERVICE_CATALOGUE_API_ENDPOINT")
sc_api_filter = os.getenv("SC_FILTER", '')
sc_api_token = os.getenv("SERVICE_CATALOGUE_API_KEY")
redis_host = os.getenv("REDIS_ENDPOINT")
redis_port = os.getenv("REDIS_PORT")
redis_tls_enabled = os.getenv("REDIS_TLS_ENABLED", 'False').lower() in ('true', '1', 't')
redis_token = os.getenv("REDIS_TOKEN", "")
redis_max_stream_length = int(os.getenv("REDIS_MAX_STREAM_LENGTH", "360"))
refresh_interval = int(os.getenv("REFRESH_INTERVAL", 60))
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()

def update_app_version(app_version, c_name, e_name):
  version_key = f'version:{c_name}:{e_name}'
  version_data={'v': app_version}
  try:
    # Get last entry to version stream
    last_entry_version = redis.xrevrange(version_key, max='+', min='-', count=1)
    if last_entry_version != []:
      last_version = last_entry_version[0][1]['v']
      # last_version_key = last_entry_version[0][0].split('-')[0]
      # last_version_key_dt = int(last_version_key) / 1000 # millisecs to secs epoch time
      # last_version_time=datetime.fromtimestamp(last_version_key_dt).strftime("%d/%m/%Y, %H:%M:%S")
    else:
      # Must be first time entry.
      redis.xadd(version_key, version_data, maxlen=200, approximate=False)
      log.debug(f"First version entry = {version_key}:{version_data}")
      return

    if last_version != app_version:
      redis.xadd(version_key, version_data, maxlen=200, approximate=False)
      log.info(f'Updating redis with new version. {version_key} = {version_data}')
  except Exception as e:
    log.error(e)

def process_env(c_name, e_name, endpoint, endpoint_type):
  output = {}
  # Redis key to use for stream
  stream_key = f'{endpoint_type}:{c_name}:{e_name}'
  stream_data = {}
  stream_data.update({'url': endpoint})
  try:
    # Override default User-Agent other gets blocked by mod security.
    headers = {'User-Agent': 'hmpps-health-ping'}
    r = requests.get(endpoint, headers=headers)

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
        update_app_version(app_version, c_name, e_name)
        break
      except (KeyError, TypeError):
        pass
      except Exception as e:
        log.error(f"{endpoint}: {type(e)} {e}")

  except Exception as e:
    log.error(e)

  try:
    redis.xadd(stream_key, stream_data, maxlen=redis_max_stream_length, approximate=False)
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
    log.info(f"Successfully connected to redis.")
  except Exception as e:
    log.critical("Unable to connect to redis.")
    raise SystemExit(e)

  sc_api_headers = {"Authorization": f"Bearer {sc_api_token}", "Content-Type":"application/json","Accept": "application/json"}

  # Test connection to Service Catalogue
  try:
    r = requests.head(f"{sc_api_endpoint}/_health", headers=sc_api_headers)
    log.info(f"Successfully connected to the Service Catalogue. {r.status_code}")
  except Exception as e:
    log.critical("Unable to connect to the Service Catalogue.")
    raise SystemExit(e) 

  sc_endpoint = f"{sc_api_endpoint}/v1/components?populate=environments{sc_api_filter}"

  while True:
    log.info(sc_endpoint)
    try:
      r = requests.get(sc_endpoint, headers=sc_api_headers)
      log.debug(r)
      if r.status_code == 200:
        j_data = r.json()["data"]
      else:
        raise Exception(f"Received non-200 response from Service Catalogue: {r.status_code}")
    except Exception as e:
      log.error(f"Unable to connect to Service Catalogue API. {e}")
    threads = list()

    # Start health endpoint. 
    httpHealth = threading.Thread(target=startHttpServer, daemon=True)
    threads.append(httpHealth)
    httpHealth.start()

    for component in j_data:
      for env in component["attributes"]["environments"]:
        c_name = component["attributes"]["name"]
        e_name = env["name"]
        if (env["url"]) and (env["monitor"] == True):
          if env["health_path"]:
            endpoint = f'{env["url"]}{env["health_path"]}'
            endpoint_type = "health"
            t_health = threading.Thread(target=process_env, args=(c_name, e_name, endpoint, endpoint_type), daemon=True)
            threads.append(t_health)
            t_health.start()
            log.info(f"Started thread for {c_name}:{endpoint_type}")
          if env["info_path"]:
            endpoint = f'{env["url"]}{env["info_path"]}'
            endpoint_type = "info"
            t_info = threading.Thread(target=process_env, args=(c_name, e_name, endpoint, endpoint_type), daemon=True)
            threads.append(t_info)
            t_info.start()
            log.info(f"Started thread for {c_name}:{endpoint_type}")
        else:
          continue

    log.debug(f"Active threads: {threading.active_count()}")
    sleep(refresh_interval)
