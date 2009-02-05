#!/usr/local/bin/python

import default, plugins, os, sys, subprocess, signal
from respond import Responder
from socket import gethostname
        
# Unlike earlier incarnations of this functionality,
# we don't rely on sharing displays but create our own for each test run.
class VirtualDisplayResponder(Responder):
    instance = None
    def __init__(self, *args):
        Responder.__init__(self, *args)
        self.displayName = None
        self.displayMachine = None
        self.displayPid = None
        self.displayProc = None
        self.guiSuites = []
        self.diag = plugins.getDiagnostics("virtual display")
        VirtualDisplayResponder.instance = self
        
    def addSuites(self, suites):
        guiSuites = filter(lambda suite : suite.getConfigValue("use_case_record_mode") == "GUI", suites)
        # On UNIX this is a virtual display to set the DISPLAY variable to, on Windows it's just a marker to hide the windows
        if os.name != "posix":
            self.setHideWindows(guiSuites)
        elif not self.displayName:
            self.setUpVirtualDisplay(guiSuites)

    def setHideWindows(self, suites):
        if len(suites) > 0 and not self.displayName:
            self.displayName = "HIDE_WINDOWS"

    def getXvfbLogDir(self, guiSuites):
        if len(guiSuites) > 0:
            return os.path.join(guiSuites[0].app.writeDirectory, "Xvfb") 
                              
    def setUpVirtualDisplay(self, guiSuites):
        machines = self.findMachines(guiSuites)
        logDir = self.getXvfbLogDir(guiSuites)
        machine, display, pid = self.getDisplay(machines, logDir)
        if display:
            self.displayName = display
            self.displayMachine = machine
            self.displayPid = pid
            self.guiSuites = guiSuites
            print "Tests will run with DISPLAY variable set to", display
        elif len(machines) > 0:
            plugins.printWarning("Failed to start virtual display on " + ",".join(machines) + " - using real display.")

    def getDisplay(self, machines, logDir):
        for machine in machines:
            displayName, pid = self.createDisplay(machine, logDir)
            if displayName:
                return machine, displayName, pid
            else:
                plugins.printWarning("Virtual display program Xvfb not available on " + machine)
        return None, None, None
    
    def findMachines(self, suites):
        allMachines = []
        for suite in suites:
            for machine in suite.getConfigValue("virtual_display_machine"):
                if not machine in allMachines:
                    allMachines.append(machine)
        return allMachines

    def notifyExtraTest(self, *args):
        # Called when a slave is given an extra test to solve
        if self.displayProc is not None and self.displayProc.poll() is not None:
            # If Xvfb has terminated, we need to restart it
            self.setUpVirtualDisplay(self.guiSuites)
            
    def notifyAllComplete(self):
        self.cleanXvfb()
    def notifyKillProcesses(self, *args):
        self.cleanXvfb()
    def cleanXvfb(self):
        if self.displayName and os.name == "posix":
            if self.displayMachine == "localhost":
                print "Killing Xvfb process", self.displayPid
                try:
                    os.kill(self.displayPid, signal.SIGTERM)
                except OSError:
                    print "Process had already terminated"
            else:
                self.killRemoteServer()
            self.displayName = None

    def killRemoteServer(self):
        self.diag.info("Getting ps output from " + self.displayMachine)
        print "Killing remote Xvfb process on", self.displayMachine, "with pid", self.displayPid
        subprocess.call([ "rsh", self.displayMachine, "kill", str(self.displayPid) ])

    def createDisplay(self, machine, logDir):
        if not self.canRunVirtualServer(machine):
            return None, None

        plugins.ensureDirectoryExists(logDir)
        startArgs = self.getVirtualServerArgs(machine, logDir)
        self.diag.info("Starting Xvfb using args " + repr(startArgs))
        self.displayProc = subprocess.Popen(startArgs, stdin=open(os.devnull), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        line = plugins.retryOnInterrupt(self.displayProc.stdout.readline)
        try:
            displayNum, pid = map(int, line.strip().split(","))
            self.displayProc.stdout.close()
            return self.getDisplayName(machine, displayNum), pid
        except ValueError: #pragma : no cover - should never happen, just a fail-safe
            print "Failed to parse line :\n " + line + self.displayProc.stdout.read()
            return None, None
            
    def getVirtualServerArgs(self, machine, logDir):
        binDir = plugins.installationDir("libexec")
        fullPath = os.path.join(binDir, "startXvfb.py")
        if machine == "localhost":
            return [ sys.executable, fullPath, logDir ]
        else:
            remotePython = self.findRemotePython()
            return [ "rsh", machine, remotePython + " -u " + fullPath + " " + logDir ]

    def findRemotePython(self):
        binDir = os.path.join(plugins.installationDir("site"),"bin")
        # In case it isn't the default, allow for a ttpython script in the installation
        localPointer = os.path.join(binDir, "ttpython")
        if os.path.isfile(localPointer):
            return localPointer
        else: # pragma : no cover -there is one in our local installation whether we like it or not...
            return "python"
        
    def getDisplayName(self, machine, displayNumber):
        # No point in using the port if we don't have to, this seems less reliable if the process is local
        # X keeps track of these numbers internally and connecting to them works rather better.
        displayStr = ":" + str(displayNumber) + ".0"
        if machine == "localhost":
            return displayStr
        else:
            return machine + displayStr

    def canRunVirtualServer(self, machine):
        # If it's not localhost, we need to make sure it exists and has Xvfb installed
        whichArgs = [ "which", "Xvfb" ]
        if machine != "localhost":
            whichArgs = [ "rsh", machine ] + whichArgs
        whichProc = subprocess.Popen(whichArgs, stdin=open(os.devnull), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outStr, errStr = whichProc.communicate()
        return len(errStr) == 0 and outStr.find("not found") == -1
