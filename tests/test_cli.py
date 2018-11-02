import unittest
import click
from click.testing import CliRunner
from giterop.__main__ import cli

class CliTest(unittest.TestCase):

  def test_help(self):
    runner = CliRunner()
    result = runner.invoke(cli, [])
    assert result.output.startswith("Usage: cli [OPTIONS] COMMAND [ARGS]"), result.output
    self.assertEqual(result.exit_code, 0)

  def test_version(self):
    runner = CliRunner()
    result = runner.invoke(cli, ['version'])
    self.assertEqual(result.exit_code, 0)
    self.assertEqual(result.output.strip(), "0.0.1alpha") #XXX use real version

  def test_run(self):
    runner = CliRunner()
    with runner.isolated_filesystem():
      with open('manifest.yaml', 'w') as f:
        f.write('invalid manifest')
      result = runner.invoke(cli, ['run'])
      self.assertEqual(result.exit_code, 1)
      self.assertEqual(result.output.strip(), "Error: malformed YAML or JSON document")