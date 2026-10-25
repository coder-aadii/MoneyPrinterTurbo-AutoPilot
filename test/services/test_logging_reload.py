import subprocess
import sys


def test_reloading_logging_module_replaces_only_terminal_sink():
    script = """
import importlib
import io
from loguru import logger
from app.utils import logging_utils as logs

terminal_sinks = []
task_sink = io.StringIO()
task_id = logger.add(task_sink, format='{message}')
for number in range(3):
    logs = importlib.reload(logs)
    terminal_sinks.append(io.StringIO())
    logs.configure_terminal_logger(terminal_sinks[-1], 'INFO', colorize=False)
    logger.info(f'event-{number}')
logger.remove(task_id)
for number in range(3):
    assert sum(f'event-{number}' in sink.getvalue() for sink in terminal_sinks) == 1
    assert f'event-{number}' in task_sink.getvalue()
print('terminal sinks replaced; task sink preserved')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "terminal sinks replaced; task sink preserved" in result.stdout
