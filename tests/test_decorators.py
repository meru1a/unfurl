import unittest
import io
from click.testing import CliRunner
from unfurl.yamlmanifest import YamlManifest
from unfurl.eval import Ref, map_value, RefContext
from unfurl.job import Runner, JobOptions

# expressions evaluate on tosca nodespecs (ignore validation errors)
# a compute instant that supports cloudinit and hosts a DockerComposeApp
# root __reflookup__ matches node templates by compatible type or template name
# nodes match relationships by requirement names
# relationships match source by compatible type or template name
class DecoratorTest(unittest.TestCase):
    def test_decorator(self):
        cliRunner = CliRunner()
        with cliRunner.isolated_filesystem():
            path = __file__ + "/../examples/decorators-ensemble.yaml"
            manifest = YamlManifest(path=path)

            ctx = RefContext(manifest.tosca.topology)
            result1 = Ref("my_server::dependency::tosca.nodes.Compute").resolve(ctx)
            self.assertEqual("my_server2", result1[0].name)

            self.assertEqual(
                {"test": "annotated"},
                manifest.tosca.nodeTemplates["my_server2"].properties,
            )
            for name in ["anode", "anothernode"]:
                node = manifest.tosca.nodeTemplates[name]
                self.assertEqual(
                    {"ports": [], "private_address": "annotated", "imported": "foo"},
                    node.properties,
                )
            assert {"foo": "bar"} == (
                manifest.tosca.template.tpl["topology_template"]["node_templates"][
                    "node3"
                ]["requirements"][0]["a_connection"]["relationship"]["properties"]
            )

            # run job so we generate instances
            # set out we don't save the file
            Runner(manifest).run(JobOptions(out=io.StringIO()))
            assert manifest.rootResource.instances

            result = manifest.rootResource.query("::my_server::.sources::a_connection")
            assert result and result.name == "node3"
            result2 = manifest.rootResource.query("::my_server::.targets::dependency")
            assert result2 and result2.name == "my_server2"
