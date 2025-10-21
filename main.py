#!/usr/bin/env python3
"""
HMPPS Slack Relay Bot

A Python application that listens to messages on specific Slack channels
and forwards them to other channels based on configurable mappings.
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


def main():
  """Main entry point for the application."""

  # Environment variable takes precedence over config file
  log_level = os.getenv('LOG_LEVEL', 'INFO')

  # Setup logging
  setup_logging(log_level)

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
