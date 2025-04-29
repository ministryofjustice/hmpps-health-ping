import logging
import os

log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
class Jobs:
  def __init__(self):
    self.error_messages = []
    self.name = None

  logging.basicConfig(
    format='[%(asctime)s] %(levelname)s %(threadName)s %(message)s',
    level=os.getenv('LOG_LEVEL', 'INFO').upper(),
  )
  log = logging.getLogger(__name__)

# Create a global shared instance
job = Jobs()

def log_info(message: str):
  job.log.info(f"{message}")

def log_error(error_message: str):
  job.error_messages.append(error_message)
  job.log.error(f"{error_message}")

def log_critical(error_message: str):
  job.error_messages.append(error_message)
  job.log.critical(f"{error_message}")

def log_warning(message: str):
  job.log.warning(f"{message}")

def log_debug(message: str):
  job.log.debug(f"{message}")