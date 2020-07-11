import unittest
from unfurl.yamlmanifest import YamlManifest
from unfurl.job import Runner, JobOptions

manifest = """
apiVersion: unfurl/v1alpha1
kind: Manifest
spec:
  service_template:
    imports:
      - repository: unfurl
        file: configurators/helm-template.yaml

    topology_template:
      node_templates:
        stable_repo:
          type: unfurl.nodes.HelmRepository
          properties:
            name: stable
            url:  http://localhost:8010/fixtures/helmrepo/

        k8sNamespace:
         type: unfurl.nodes.K8sNamespace
         # requirements:
         #   - host: k8sCluster
         properties:
           name: unfurl-helm-unittest

        mysql_release:
          type: unfurl.nodes.HelmRelease
          requirements:
            - repository:
                node: stable_repo
            - host:
                node: k8sNamespace
          properties:
            chart: stable/mysql
            release_name: mysql-test
"""

import threading
import os.path
from functools import partial

# http://localhost:8000/fixtures/helmrepo
class HelmTest(unittest.TestCase):
    def setUp(self):
        server_address = ("", 8010)
        directory = os.path.dirname(__file__)
        try:
            from http.server import HTTPServer, SimpleHTTPRequestHandler

            handler = partial(SimpleHTTPRequestHandler, directory=directory)
            self.httpd = HTTPServer(server_address, handler)
        except ImportError:  # for python 2.7
            from SimpleHTTPServer import SimpleHTTPRequestHandler
            import SocketServer
            import urllib

            class RootedHTTPRequestHandler(SimpleHTTPRequestHandler):
                def translate_path(self, path):
                    path = os.path.normpath(urllib.unquote(path))
                    words = path.split("/")
                    words = filter(None, words)
                    path = directory
                    for word in words:
                        drive, word = os.path.splitdrive(word)
                        head, word = os.path.split(word)
                        if word in (os.curdir, os.pardir):
                            continue
                        path = os.path.join(path, word)
                    return path

            self.httpd = SocketServer.TCPServer(
                server_address, RootedHTTPRequestHandler
            )

        t = threading.Thread(name="http_thread", target=self.httpd.serve_forever)
        t.daemon = True
        t.start()

    def tearDown(self):
        self.httpd.socket.close()

    def test_helm(self):
        runner = Runner(YamlManifest(manifest))

        run1 = runner.run(JobOptions(dryrun=False, verbose=3, startTime=1))
        assert not run1.unexpectedAbort, run1.unexpectedAbort.getStackTrace()
        summary = run1.jsonSummary()
        self.assertEqual(
            summary["job"],
            {
                "id": "A01110000000",
                "status": "ok",
                "total": 9,
                "ok": 9,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 9,
            },
        )
        assert all(task[-1] == "ok" for task in summary["tasks"]), summary["tasks"]

        run2 = runner.run(JobOptions(workflow="undeploy", startTime=2))
        assert not run2.unexpectedAbort, run2.unexpectedAbort.getStackTrace()
        summary = run2.jsonSummary()
        # if tests fail need to run helm uninstall mysql-test  -n unfurl-helm-unittest
        self.assertEqual(
            summary["job"],
            {
                "id": "A01120000000",
                "status": "ok",
                "total": 8,
                "ok": 8,
                "error": 0,
                "unknown": 0,
                "skipped": 0,
                "changed": 8,
            },
        )
        assert all(task[-1] == "absent" for task in summary["tasks"]), summary["tasks"]