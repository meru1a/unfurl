import unittest
import os
import traceback
from click.testing import CliRunner
from unfurl.__main__ import cli
from unfurl import __version__
from git import Repo


def createUnrelatedRepo(gitDir):
    os.makedirs(gitDir)
    repo = Repo.init(gitDir)
    filename = "README"
    filepath = os.path.join(gitDir, filename)
    with open(filepath, "w") as f:
        f.write("""just another git repository""")

    repo.index.add([filename])
    repo.index.commit("Initial Commit")
    return repo


class SharedGitRepoTest(unittest.TestCase):
    """
    test that .gitignore, unfurl.local.example.yaml is created
    test that init cmd committed the project config and related files
    """

    def test_init(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            repoDir = "./arepo"
            repo = createUnrelatedRepo(repoDir)
            os.chdir(repoDir)
            result = runner.invoke(cli, ["init", "deploy_dir"])
            # uncomment this to see output:
            # print("result.output", result.exit_code, result.output)
            assert not result.exception, "\n".join(
                traceback.format_exception(*result.exc_info)
            )
            self.assertEqual(result.exit_code, 0, result)
            expectedFiles = {
                "unfurl.yaml",
                "service-template.yaml",
                ".gitignore",
                "manifest.yaml",
                "unfurl.local.example.yaml",
            }
            self.assertEqual(set(os.listdir("deploy_dir")), expectedFiles)
            self.assertEqual(
                set(repo.head.commit.stats.files.keys()),
                {"deploy_dir/" + f for f in expectedFiles},
            )