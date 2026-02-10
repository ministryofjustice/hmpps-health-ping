#!/usr/bin/env python3
"""
HMPPS Health Ping

A Python application that loops through the service catalogue
to check health of components and logs the results to a Redis database.

"""

import os
import logging

from health_ping import HealthPing
from hmpps import Slack, GithubSession, ServiceCatalogue, HealthServer
from hmpps.services.job_log_handling import (
  log_debug,
  log_info,
  log_critical,
  job,
)
import redis


def setup_logging(level: str = 'INFO') -> None:
  """Setup logging configuration."""
  logging.basicConfig(
    level=getattr(logging, level.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
  )


class Services:
  def __init__(self):
    self.slack = Slack()
    self.sc = ServiceCatalogue()
    self.gh = GithubSession()
    self.redis_max_stream_length = int(os.getenv('REDIS_MAX_STREAM_LENGTH', '360'))
    self.redis = self.connect_to_redis()

  def connect_to_redis(self):
    redis_params = {
      'host': os.getenv('REDIS_ENDPOINT', ''),
      'port': os.getenv('REDIS_PORT', ''),
      'ssl': os.getenv('REDIS_TLS_ENABLED', 'False').lower()
      in (
        'true',
        '1',
        't',
      ),
      'password': os.getenv('REDIS_TOKEN', ''),
      'ssl_cert_reqs': None,
      'decode_responses': True,
    }

    # Test connection to redis
    try:
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


def main():
  """Main entry point for the application."""

  # Environment variable takes precedence over config file
  log_level = os.getenv('LOG_LEVEL', 'INFO')

  # Setup logging
  setup_logging(log_level)

  job.name = 'hmpps-health-ping'

  services = Services()

  logger = logging.getLogger(__name__)
  logger.info('Starting HMPPS Health Ping')

  health_server = HealthServer()
  health_server.start()

  # Initiate the health_ping class
  health_ping = HealthPing(services)
  # Initialize and start the health ping
  try:
    health_ping.start_health_ping()

  except KeyboardInterrupt:
    logger.info('Received interrupt signal, shutting down gracefully...')
  except (ConnectionError, OSError, PermissionError) as e:
    logger.error('An error occurred: %s', e)
    return 1

  return 0


if __name__ == '__main__':
  exit(main())
