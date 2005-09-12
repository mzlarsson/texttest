#!/usr/local/bin/python

import os, filecmp, string, plugins, signal, copy

plugins.addCategory("badPredict", "internal errors", "had internal errors")
plugins.addCategory("crash", "CRASHED")
plugins.addCategory("killed", "killed", "were terminated before completion")

# For backwards compatibility...
class FailedPrediction(plugins.TestState):
    def isSaveable(self):
        # for back-compatibility
        return 1

class CheckLogFilePredictions(plugins.Action):
    def __init__(self, version = None):
        self.logFile = None
        self.version = version
    def getLogFile(self, test, stem):
        logFile = test.makeFileName(stem, self.version, temporary=1)
        if not os.path.isfile(logFile):
            logFile = test.makeFileName(stem, self.version)
            if not os.path.isfile(logFile):
                return None
        return logFile
    def insertError(self, test, errType, briefError, error=""):
        test.changeState(FailedPrediction(errType, briefText=briefError, freeText=error, started=1, executionHosts=test.state.executionHosts))
    def setUpApplication(self, app):
        self.logFile = app.getConfigValue("log_file")   

class CheckPredictions(CheckLogFilePredictions):
    def __init__(self, version = None):
        CheckLogFilePredictions.__init__(self, version)
        self.internalErrorList = None
        self.internalCompulsoryList = None
    def __repr__(self):
        return "Checking predictions for"
    def __call__(self, test):
        if not test.state.isComplete():
            self.collectErrors(test)
    def findCrashSummary(self, errInfo):
        prevLine = ""
        crashType = "CRASH"
        for line in errInfo.split("\n"):
            if line.find("Program terminated with") != -1:
                crashType = line.split(",")[-1].strip().replace(".", "")
            if prevLine.find("Stack trace from") != -1:
                return crashType + " in " + line.strip()
            prevLine = line
        return "unknown " + crashType
    def collectErrors(self, test):
        if plugins.emergencySignal:
            briefText, freeText = self.getKillText(plugins.emergencySignal)
            self.insertError(test, "killed", briefText, freeText + "\n")
            return 1
        # Hard-coded prediction: check test didn't crash
        stackTraceFile = test.makeFileName("stacktrace", temporary=1)
        if os.path.isfile(stackTraceFile):
            errorInfo = open(stackTraceFile).read()
            summary = self.findCrashSummary(errorInfo)
            if summary.startswith("unknown") and len(errorInfo) > 50000:
                errorInfo = "Stack trace contained over 50000 characters, suspecting binary output problems..."
            self.insertError(test, "crash", summary, errorInfo)
            os.remove(stackTraceFile)
            return 1

        if len(self.internalErrorList) == 0 and len(self.internalCompulsoryList) == 0:
            return 0
                
        compsNotFound = copy.deepcopy(self.internalCompulsoryList)
        errorsFound = self.extractErrorsFrom(test, self.logFile, compsNotFound)
        errorsFound += self.extractErrorsFrom(test, "errors", compsNotFound)
        errorsFound += len(compsNotFound)
        for comp in compsNotFound:
            self.insertError(test, "badPredict", "missing '" + comp + "'")
        return errorsFound
    def getKillText(self, sig):
        if sig == signal.SIGUSR1:
            return "RUNLIMIT", "Test exceeded maximum wallclock time allowed"
        elif sig == signal.SIGXCPU:
            return "CPULIMIT", "Test exceeded maximum cpu time allowed"
        elif sig == signal.SIGUSR2:
            timeStr = plugins.localtime("%H:%M")
            return "killed at " + timeStr, "Test killed explicitly at " + timeStr
        else:
            return "signal " + str(sig), "Terminated by signal " + str(sig)
    def extractErrorsFrom(self, test, fileStem, compsNotFound):
        errorsFound = 0
        logFile = self.getLogFile(test, fileStem)
        if not logFile:
            return 0
        for line in open(logFile).xreadlines():
            for error in self.internalErrorList:
                if line.find(error) != -1:
                    errorsFound += 1
                    self.insertError(test, "badPredict", error)
            for comp in compsNotFound:
                if line.find(comp) != -1:
                    compsNotFound.remove(comp)
        return errorsFound
    def setUpApplication(self, app):
        CheckLogFilePredictions.setUpApplication(self, app)
        self.internalErrorList = app.getConfigValue("internal_error_text")
        self.internalCompulsoryList = app.getConfigValue("internal_compulsory_text")

def pad(str, padSize):
    return str.ljust(padSize)
        
class PredictionStatistics(plugins.Action):
    def __init__(self, args=[]):
        versions = self.getVersions(args)
        self.referenceChecker = CheckPredictions(versions[0])
        self.currentChecker = None
        if len(versions) > 1:
            self.currentChecker = CheckPredictions(versions[1])
    def getVersions(self, args):
        if len(args) == 0:
            return [""]
        arg, val = args[0].split("=")
        return val.split(",")
    def setUpSuite(self, suite):
        self.suiteName = suite.name + "\n   "
    def scriptDoc(self):
        return "Displays statistics about application internal errors present in the test suite"
    def __call__(self, test):
        refErrors = self.referenceChecker.collectErrors(test)
        currErrors = 0
        if self.currentChecker:
            currErrors = self.currentChecker.collectErrors(test)
        if refErrors + currErrors > 0:
            print self.suiteName + test.name.ljust(30) + "\t", refErrors, currErrors
            self.suiteName = "   "
    def setUpApplication(self, app):
        self.referenceChecker.setUpApplication(app)
        if self.currentChecker:
            self.currentChecker.setUpApplication(app)
