
import vcs_independent, plugins, shutil, datetime, time, os

class HgInterface(vcs_independent.VersionControlInterface):
    def __init__(self, controlDir):
        self.warningStateInfo = { "M": "Modified", "R":"Removed", "A":"Added" }
        self.errorStateInfo = { "?" : "Unknown", "!" : "Missing" }
        self.allStateInfo = { "C": "Unchanged", "I": "Ignored" }
        self.allStateInfo.update(self.warningStateInfo)
        self.allStateInfo.update(self.errorStateInfo)
        vcs_independent.VersionControlInterface.__init__(self, controlDir, "Mercurial",
                                                         self.warningStateInfo.values(), self.errorStateInfo.values(), "tip")
        self.defaultArgs["rm"] = [ "--force" ]
        self.defaultArgs["status"] = [ "-A" ] # Otherwise we can't tell difference between Unchanged and Ignored
        self.defaultArgs["log"] = [ "-f" ] # "follow" renames, which isn't default
        self.defaultArgs["annotate"] = [ "-f", "-n" ] # annotate -f doesn't annotate anything...
        self.defaultArgs["diff"] = [ "-g" ] # "git format", which apparently is how to make it work with revisions :)
        
    def getDateFromLog(self, output):
        for line in output.splitlines():
            if line.startswith("date:"):
                dateStr = " ".join(line.split()[2:-1])
                return datetime.datetime(*(self.parseDateTime(dateStr)[0:6]))

    def getGraphicalDiffArgs(self, diffProgram):
        return [ "hg", "extdiff", "-p", diffProgram ]

    def parseDateTime(self, input):
        return time.strptime(input, "%b %d %H:%M:%S %Y")

    def getStateFromStatus(self, output):
        statusLetter = output.split()[0]
        return self.allStateInfo.get(statusLetter, statusLetter)

    def _movePath(self, oldPath, newPath):
        # Moving doesn't work in hg if there are symbolic links in the path to the new location!
        retCode = self.callProgram("mv", [ oldPath, os.path.realpath(newPath) ])
        # And it doesn't take non-versioned files with it, if there are any...
        if retCode == 0 and os.path.isdir(oldPath):
            self.copyPath(oldPath, newPath)
            shutil.rmtree(oldPath)
            
    def removePath(self, path):
        # Mercurial doesn't remove unknown files, nor does it return non-zero exit codes when it fails
        retValue = os.path.exists(path)
        self.callProgram("rm", [ path ]) # returns 0 whatever happens
        plugins.removePath(path)
        return retValue


vcs_independent.vcsClass = HgInterface

class AnnotateGUI(vcs_independent.AnnotateGUI):
    def commandHadError(self, retcode, stderr):
        return len(stderr) > 0 # Mercurial doesn't do return codes in annotate for some reason...

class AnnotateGUIRecursive(AnnotateGUI):
    recursive = True

#
# Configuration for the Interactive Actions
#
class InteractiveActionConfig(vcs_independent.InteractiveActionConfig):
    def annotateClasses(self):
        return [ AnnotateGUI, AnnotateGUIRecursive ]
