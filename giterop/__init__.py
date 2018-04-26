from .run import *
from .manifest import *


"""
Basic operation of GitErOp is to apply the specified configuration to a resource
and record the results of the application.

A manifest can contain a reproducible history of changes to a resource.
This history is stored in the resource definition so it doesn't
rely on git history for this.
But the intent is for commits in a git repo to correspond to reproducible configuration states of the system.
The git repo is also used to record or archive exact versions of each configurators applied.
"""


#cmd=update find and run configurations that need to be applied
#for running and recording adhoc changes:
#cmd=add resource configuration action params
def run(manifestPath, opts=None):
  manifest = Manifest(path=manifestPath)
  runner = Runner(manifest)
  kw = opts or {}
  runner.run(**kw)
