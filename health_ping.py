#!/usr/bin/env python
"""Health ping - fetches all /health and /info endpoints
and stores the results in Redis"""

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
import os
import threading
import requests
import json
from time import sleep
from redis.exceptions import LockError

from hmpps.services.job_log_handling import (
  log_debug,
  log_error,
  log_info,
  log_warning,
  job,
)

refresh_interval = int(os.getenv('REFRESH_INTERVAL', '60'))
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()

# A list of tuples of environment field names mapped to redis field names
endpoints_list = [('health_path', 'health'), ('info_path', 'info')]


class HealthPing:
  def __init__(self, services):
    self.services = services
    redis_max_connections = max(1, getattr(self.services, 'redis_max_connections', 80))
    redis_connection_headroom = max(
      0, getattr(self.services, 'redis_connection_headroom', 20)
    )
    self.worker_limit = max(1, redis_max_connections - redis_connection_headroom)
    self.wait_timeout_per_batch = int(os.getenv('WAIT_TIMEOUT_PER_BATCH', '25'))
    self.executor = ThreadPoolExecutor(
      max_workers=self.worker_limit,
      thread_name_prefix='health-ping',
    )
    log_info(
      f'Configured worker pool: {self.worker_limit} '
      f'(REDIS_MAX_CONNECTIONS={redis_max_connections}, '
      f'REDIS_CONNECTION_HEADROOM={redis_connection_headroom})'
    )

  @staticmethod
  def _is_lock_ownership_error(exc):
    return "no longer owned" in str(exc).lower()

  def _get_build_image_tag(self, output):
    version = ''
    version_locations = (
      # all apps should have version here
      "output['build']['version']",
      # Java/Kotlin springboot apps
      "output['components']['healthInfo']['details']['version']",
      # Node/Typescript apps
      "output['build']['buildNumber']",
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

  def _git_compare_commits(self, github_repo, from_sha, to_sha, services):
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
        f'Error retreiving commits for repo: {github_repo} '
        f'between {from_sha} and {to_sha} : {e}'
      )
    return comparison

  def _get_http_endpoint(self, endpoint):
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
        # Legitimate error, because Redis is exhibiting a fault
        log_error(
          f'{endpoint}: Unable to update stream_data with json - exception: {e}'
        )

      stream_data.update({'http_s': r.status_code})
      log_info(f'http response: {r.status_code}: {endpoint}')

    except requests.exceptions.RequestException as e:
      # Set status code to 0 for failed connections
      stream_data.update({'http_s': 0})
      # Log error in stream for easier diagnosis of problems
      stream_data.update({'error': str(e)})
      log_warning(f'Failed to get data from {endpoint} - exception: {e}')
    except Exception as e:
      log_warning(f'Failed to parse response from {endpoint} - exception: {e}')
    return output, stream_data

  def _get_active_agencies(self, env, output, endpoint_type):
    active_agencies_dict = {}
    try:
      if ('activeAgencies' in output) and (endpoint_type == 'info'):
        active_agencies = output['activeAgencies']

        log_info(f'SC active_agencies: {env["active_agencies"]}')
        log_info(f'Existing active_agencies: {active_agencies}')

        # if current active_agencies is empty/None set to empty list for comparison.
        cur_active_agencies = []
        if env.get('active_agencies'):
          cur_active_agencies = env['active_agencies']
        # Test if active_agencies has changed, and update SC if so.
        if sorted(active_agencies) != sorted(cur_active_agencies):
          active_agencies_dict = {'active_agencies': active_agencies}
    except (KeyError, TypeError):
      pass
    except Exception as e:
      log_warning(f'failed to process active_agencies: {e}')
    return active_agencies_dict

  def _update_app_version(
    self, app_version, update_version_history, c_name, e_name, github_repo, services
  ):
    log_debug(f'Starting update_app_version for {c_name}-{e_name}')
    version_key = f'version:{c_name}:{e_name}'
    version_data = {
      'v': app_version,
      'dateAdded': datetime.now(timezone.utc).isoformat(),
    }

    lock = services.redis.lock(
      f'{c_name}_{e_name}', timeout=30, blocking=True, blocking_timeout=10
    )

    def _safe_release_lock():
      try:
        if lock.owned():
          lock.release()
      except LockError as e:
        log_warning(f'Unable to safely release redis lock for {version_key}: {e}')
      except Exception as e:
        if self._is_lock_ownership_error(e):
          log_warning(
            f'Redis lock ownership changed while releasing {version_key}: {e}'
          )
        else:
          log_warning(f'Unexpected lock release issue for {version_key}: {e}')

    previous_deployed_version_sha = None
    should_add_version_history = False
    should_add_first_history_entry = False

    try:
      # Read current version history in a short locked section.
      if update_version_history:
        if not lock.acquire(blocking=True, blocking_timeout=10):
          log_warning(
            f'Could not acquire redis lock for {version_key}, skipping history update.'
          )
        else:
          log_debug(f'Got lock for version history read: {lock.name}')
          try:
            versions_history = services.redis.xrevrange(
              version_key, max='+', min='-', count=10
            )

            if versions_history:
              previous_deployed_version_key = False
              for i, v in enumerate(versions_history):
                if v[1]['v'] != app_version:
                  previous_deployed_version_key = i
                  break

              latest_version_from_redis = versions_history[0][1]['v']
              if latest_version_from_redis != app_version:
                should_add_version_history = True
                if isinstance(previous_deployed_version_key, int):
                  previous_deployed_version = versions_history[
                    previous_deployed_version_key
                  ][1]['v']
                  previous_deployed_version_sha = previous_deployed_version.split('.')[-1]
            else:
              should_add_first_history_entry = True
          finally:
            _safe_release_lock()

        # Fetch git compare outside the lock; this call can be slow.
        if should_add_version_history and previous_deployed_version_sha:
          app_version_sha = app_version.split('.')[-1]
          commits = self._git_compare_commits(
            github_repo, previous_deployed_version_sha, app_version_sha, services
          )
          log_info(f'Fetching commits for build: {app_version}')
          version_data.update({'git_compare': json.dumps(commits)})

        # Write history update in a second short locked section.
        if should_add_version_history or should_add_first_history_entry:
          if not lock.acquire(blocking=True, blocking_timeout=10):
            log_warning(
              f'Could not acquire redis lock for {version_key}, skipping history write.'
            )
          else:
            log_debug(f'Got lock for version history write: {lock.name}')
            try:
              latest_version_history = services.redis.xrevrange(
                version_key, max='+', min='-', count=1
              )

              if (not latest_version_history) or (
                latest_version_history[0][1]['v'] != app_version
              ):
                services.redis.xadd(
                  version_key, version_data, maxlen=200, approximate=False
                )
                log_info(
                  f'Updated redis stream with new version. '
                  f'{version_key} = {version_data}'
                )
              else:
                log_debug(
                  f'Skipping version stream update for {version_key}; '
                  f'latest entry already equals {app_version}'
                )
            finally:
              _safe_release_lock()

      # Always update the latest version key
      services.redis.json().set('latest:versions', f'$.{version_key}', version_data)
      log_info(
        f'Updated latest:versions redis key with latest version. '
        f'{version_key} = {version_data}'
      )
    except LockError as e:
      log_warning(f'Redis lock issue during version update for {version_key}: {e}')
    except Exception as e:
      if self._is_lock_ownership_error(e):
        log_warning(
          f'Redis lock ownership changed during version update for {version_key}: {e}'
        )
      else:
        log_error(f'Failed to update redis versions - {e}')
    finally:
      _safe_release_lock()

    log_debug(f'Completed update_app_version for {c_name}-{e_name}')

  def _process_env(self, c_name, component, env_id, env, services):
    log_info(f'Processing {component.get("name")} {env.get("name")}')

    # variables to store just once for all attributes
    app_version = None
    update_version_history = False

    # Redis key to use for stream
    e_name = env.get('name')
    log_debug(f'environment name e_name={e_name}')

    # main loop for the endpoint types
    for endpoint_tuple in endpoints_list:
      if endpoint_uri := env.get(endpoint_tuple[0]):
        endpoint = f'{env["url"]}{endpoint_uri}'
        endpoint_type = endpoint_tuple[1]
        log_debug(f'endpoint: {endpoint}')
        stream_key = f'{endpoint_type}:{c_name}:{e_name}'
        log_debug(f'stream_key={stream_key}')
        stream_data = {}
        stream_data.update({'url': endpoint})
        stream_data.update({'dateAdded': datetime.now(timezone.utc).isoformat()})

        # Get component id
        c_id = component.get('documentId', '')

        # make the call to the endpoint
        output, stream_updated_data = self._get_http_endpoint(endpoint)
        if stream_updated_data:
          stream_data.update(stream_updated_data)

        # Try to get app version (HEAT-567 -
        # get app version from build image tag on health or info)
        env_data = {}
        update_sc = False
        if output_app_version := self._get_build_image_tag(output):
          # Only update app_version if an valid image tag is returned
          app_version = output_app_version
          log_debug(
            f'Found app version: {c_name}:{e_name}:{app_version} in {endpoint_type}'
          )

          # update the build image tag in the environment data if it's different
          current_image_tag = env.get('build_image_tag', '')
          log_debug((f'existing build_image_tag: {current_image_tag}'))
          if app_version != current_image_tag:
            env_data.update({'build_image_tag': app_version})
            update_sc = True
            log_info(
              f'Updating Service Catalogue build_image_tag for component '
              f'{c_id} {c_name} - Environment {env_id} {e_name}{env_data}'
            )
            update_version_history = True
          else:
            log_debug(
              f'No change in service catalogue build_image_tag for component {c_id} '
              f'{c_name} - Environment {env_id} {e_name}'
            )
        # No app_version data found in this endpoint
        else:
          log_info(
            f'No app_version data in {endpoint_tuple[1]} endpoint '
            f'for {c_name} {env.get("name")}'
          )

        # Try to get active agencies
        if active_agencies := self._get_active_agencies(output, env, endpoint_type):
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
          if 'too many connections' in str(e).lower():
            log_warning(
              f'Redis connection capacity reached while writing {stream_key}: {e}. '
              'Reduce worker concurrency or increase REDIS_MAX_CONNECTIONS.'
            )
          else:
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
        f'app_version:({app_version}) and '
        f'update_version_history is {update_version_history}'
      )
      github_repo = component.get('github_repo', '')
      self._update_app_version(
        app_version, update_version_history, c_name, e_name, github_repo, services
      )
    else:
      log_debug('no app version')

  def _get_components(self):
    return self.services.sc.get_all_records(
      f'{self.services.sc.components_get}&filters[archived][$eq]=false'
    )

  def _iter_monitored_envs(self, components):
    for component in components:
      c_name = component.get('name')
      for env in component.get('envs', []):
        if env.get('url') and env.get('monitor'):
          yield c_name, component, env.get('documentId', ''), env
      log_debug(f'Active threads: {threading.active_count()}')

  def _submit_env_jobs(self, components):
    futures = []
    for c_name, component, env_id, env in self._iter_monitored_envs(components):
      future = self.executor.submit(
        self._process_env,
        c_name,
        component,
        env_id,
        env,
        self.services,
      )
      futures.append(future)
      log_info(
        f'Submitted task for {c_name} {env.get("name")} '
        f'(active threads: {threading.active_count()})'
      )
    return futures

  def _wait_for_futures(self, futures, timeout=30):
    if not futures:
      return 0, timeout

    batches = (len(futures) + self.worker_limit - 1) // self.worker_limit
    dynamic_timeout = max(timeout, batches * self.wait_timeout_per_batch)
    done, not_done = wait(futures, timeout=dynamic_timeout)

    for future in done:
      try:
        future.result()
      except Exception as e:
        log_error(f'Worker task failed: {e}')

    still_running = 0
    cancelled = 0
    for future in not_done:
      if future.cancel():
        cancelled += 1
      elif future.running():
        still_running += 1

    pending = len(not_done) - cancelled - still_running
    if not_done:
      log_warning(
        f'Wait timeout reached after {dynamic_timeout}s: '
        f'running={still_running}, pending={pending}, cancelled={cancelled}.'
      )

    return still_running, dynamic_timeout

  # Deal with any stuck threads (send a Slack message)
  def _handle_stuck_threads(self, stuck_threads, total_threads, timeout_seconds):
    if stuck_threads > 0:
      log_warning(
        f'{stuck_threads} worker thread(s) still running before sleep '
        f'(started this run: {total_threads})'
      )
      if stuck_threads > 1:
        try:
          self.services.slack.alert(
            f'*{job.name} encountered stuck threads *: '
            f'{stuck_threads} worker thread(s) still running after '
            f'{timeout_seconds}s timeout.'
          )
        except Exception as e:
          log_error(f'Unable to send Slack alert for stuck threads. {e}')
    else:
      log_info('No stuck worker threads before sleep.')

  def _update_job_result(self):
    # Even if job had errors , error will be recorded in SC
    # and job will be marked successful
    # as few services are expected to fail.
    if job.error_messages:
      self.services.sc.update_scheduled_job('Errors')
      log_info(f'{job.name} job completed with errors.')
    else:
      self.services.sc.update_scheduled_job('Succeeded')
      log_info(f'{job.name} job completed successfully.')

  # Main health ping threads dispatcher
  def _run_health_ping_once(self):
    components = self._get_components()
    futures = self._submit_env_jobs(components)
    stuck_threads, timeout_seconds = self._wait_for_futures(futures)
    self._handle_stuck_threads(stuck_threads, len(futures), timeout_seconds)
    log_info(f'Completed all threads. Sleeping for {refresh_interval} seconds.')
    self._update_job_result()

  def start_health_ping(self):
    while True:
      log_info('Starting a new run.')
      try:
        self._run_health_ping_once()
      except Exception as e:
        log_error(f'Unexpected error in health ping loop: {e}')
        self.services.sc.update_scheduled_job('Errors')
      finally:
        # Clear error messages for the next run
        job.error_messages.clear()
        log_info(f'Sleeping for {refresh_interval} seconds...')
        sleep(refresh_interval)
