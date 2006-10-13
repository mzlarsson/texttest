#!/usr/bin/env python

# GUI for TextTest written with PyGTK
# First make sure we can import the GUI modules: if we can't, throw appropriate exceptions

def raiseException(msg):
    from plugins import TextTestError
    raise TextTestError, "Could not start TextTest GUI due to PyGTK GUI library problems :\n" + msg

try:
    import gtk
except:
    raiseException("Unable to import module 'gtk'")

major, minor, debug = gtk.pygtk_version
if major < 2 or minor < 4:
    raiseException("TextTest GUI requires at least PyGTK 2.4 : found version " + str(major) + "." + str(minor))

try:
    import gobject
except:
    raiseException("Unable to import module 'gobject'")

import guiplugins, plugins, comparetest, os, string, time, sys, locale
from threading import Thread, currentThread
from gtkusecase import ScriptEngine, TreeModelIndexer
from ndict import seqdict
from respond import Responder

def destroyDialog(dialog, *args):
    dialog.destroy()

def showError(message):
    guilog.info("ERROR : " + message)
    dialog = gtk.Dialog("TextTest Message", buttons=(gtk.STOCK_OK, gtk.RESPONSE_ACCEPT))
    dialog.set_modal(True)
    label = gtk.Label(message)
    dialog.vbox.pack_start(label, expand=True, fill=True)
    label.show()
    scriptEngine.connect("agree to texttest message", "response", dialog, destroyDialog, gtk.RESPONSE_ACCEPT)
    dialog.show()    

class DoubleCheckDialog:
    def __init__(self, message, yesMethod, yesMethodArgs=()):
        self.dialog = gtk.Dialog("TextTest Query", flags=gtk.DIALOG_MODAL)
        self.yesMethod = yesMethod
        self.yesMethodArgs = yesMethodArgs
        guilog.info("QUERY : " + message)
        noButton = self.dialog.add_button(gtk.STOCK_NO, gtk.RESPONSE_NO)
        yesButton = self.dialog.add_button(gtk.STOCK_YES, gtk.RESPONSE_YES)
        self.dialog.set_modal(True)
        label = gtk.Label(message)
        self.dialog.vbox.pack_start(label, expand=True, fill=True)
        label.show()
        # ScriptEngine cannot handle different signals for the same event (e.g. response
        # from gtk.Dialog), so we connect the individual buttons instead ...
        scriptEngine.connect("answer no to texttest query", "clicked", noButton, self.respond, gtk.RESPONSE_NO, False)
        scriptEngine.connect("answer yes to texttest query", "clicked", yesButton, self.respond, gtk.RESPONSE_YES, True)
        self.dialog.show()
    def respond(self, button, saidYes, *args):
        if saidYes:
            self.yesMethod(*self.yesMethodArgs)
        self.dialog.destroy()

def renderParentsBold(column, cell, model, iter):
    if model.iter_has_child(iter):
        cell.set_property('font', "bold")
    else:
        cell.set_property('font', "")

def renderSuitesBold(column, cell, model, iter):
    if model.get_value(iter, 2).classId() == "test-case":
        cell.set_property('font', "")
    else:
        cell.set_property('font', "bold")

def getTestColour(test, category):
    colours = test.getConfigValue("test_colours")
    if colours.has_key(category):
        return colours[category]
    else:
        # Everything unknown is assumed to be a new type of failure...
        return colours["failure"]

class QuitGUI(guiplugins.SelectionAction):
    def __init__(self, rootSuites, dynamic, topWindow, actionThread):
        guiplugins.SelectionAction.__init__(self, rootSuites)
        self.dynamic = dynamic
        self.topWindow = topWindow
        self.actionThread = actionThread
        scriptEngine.connect("close window", "delete_event", topWindow, self.exit)
    def getInterfaceDescription(self):
        description = "<menubar>\n<menu action=\"filemenu\">\n<menuitem action=\"" + self.getSecondaryTitle() + "\"/>\n</menu>\n</menubar>\n"
        description += "<toolbar>\n<toolitem action=\"" + self.getSecondaryTitle() + "\"/>\n<separator/>\n</toolbar>\n"
        return description
    def getStockId(self):
        return "quit"
    def getAccelerator(self):
        return "<control>q"
    def getTitle(self):
        return "_Quit"
    def messageBeforePerform(self, testSel):
        return "Terminating TextTest GUI ..."
    def messageAfterPerform(self, testSel):
        # Don't provide one, the GUI isn't there to show it :)
        pass
    def performOn(self, tests, files):
        # Generate a window closedown, so that the quit button behaves the same as closing the window
        self.exit()
    def getDoubleCheckMessage(self, test):
        processesToReport = self.processesToReport()
        runningProcesses = guiplugins.processTerminationMonitor.listRunning(processesToReport)
        if len(runningProcesses) == 0:
            return ""
        else:
            return "\nThese processes are still running, and will be terminated when quitting: \n\n   + " + string.join(runningProcesses, "\n   + ") + "\n\nQuit anyway?\n"
    def processesToReport(self):
        queryValues = self.getConfigValue("query_kill_processes")
        processes = []
        if queryValues.has_key("default"):
            processes += queryValues["default"]
        if self.dynamic and queryValues.has_key("dynamic"):
            processes += queryValues["dynamic"]
        elif queryValues.has_key("static"):        
            processes += queryValues["static"]
        return processes
    def exit(self, *args):
        # block all event notifications to make sure GUI isn't updated after being destructed
        plugins.Observable.blocked = True
        self.topWindow.destroy()
        gtk.main_quit()
        sys.stdout.flush()
        if self.actionThread:
            self.actionThread.terminate()
        guiplugins.processTerminationMonitor.killAll()    

def getGtkRcFile():
    configDir = plugins.getPersonalConfigDir()
    if not configDir:
        return
    
    file = os.path.join(configDir, ".texttest_gtk")
    if os.path.isfile(file):
        return file

class TextTestGUI(Responder):
    defaultGUIDescription = '''
<ui>
  <menubar>
  </menubar>
  <toolbar>
  </toolbar>
</ui>
'''
    def __init__(self, optionMap):
        self.readGtkRCFile()
        self.dynamic = not optionMap.has_key("gx")
        Responder.__init__(self, optionMap)
        guiplugins.scriptEngine = self.scriptEngine
        self.rightWindowGUI = None
        self.selectionActionGUI = None
        self.testTreeGUI = TestTreeGUI(self.dynamic)
        self.contents = None
        self.progressMonitor = None
        self.progressBar = None
        self.toolTips = gtk.Tooltips()
        self.rootSuites = []
        self.status = GUIStatusMonitor()
        
        # Create GUI manager, and a few default action groups
        self.uiManager = gtk.UIManager()
        basicActions = gtk.ActionGroup("Basic")
        basicActions.add_actions([("filemenu", None, "_File"), ("actionmenu", None, "_Actions")])
        self.uiManager.insert_action_group(basicActions, 0)
        self.uiManager.insert_action_group(gtk.ActionGroup("Suite"), 1)
        self.uiManager.insert_action_group(gtk.ActionGroup("Case"), 2)
    def needsOwnThread(self):
        return True
    def readGtkRCFile(self):
        file = getGtkRcFile()
        if file:
            gtk.rc_add_default_file(file)
    def setUpScriptEngine(self):
        guiplugins.setUpGuiLog(self.dynamic)
        global guilog, scriptEngine
        from guiplugins import guilog
        scriptEngine = ScriptEngine(guilog, enableShortcuts=1)
        self.scriptEngine = scriptEngine
    def needsTestRuns(self):
        return self.dynamic
    def createTopWindow(self):
        # Create toplevel window to show it all.
        win = gtk.Window(gtk.WINDOW_TOPLEVEL)
        if self.dynamic:
            win.set_title("TextTest dynamic GUI (tests started at " + plugins.startTimeString() + ")")
        else:
            win.set_title("TextTest static GUI : management of tests for " + self.getAppNames())
            
        guilog.info("Top Window title set to " + win.get_title())
        win.add_accel_group(self.uiManager.get_accel_group())
        return win
    def getAppNames(self):
        names = []
        for suite in self.rootSuites:
            if not suite.app.fullName in names:
                names.append(suite.app.fullName)
        return string.join(names, ",")
    def fillTopWindow(self, topWindow, testWins, rightWindow):
        mainWindow = self.createWindowContents(testWins, rightWindow)

        vbox = gtk.VBox()
        self.placeTopWidgets(vbox)
        vbox.pack_start(mainWindow, expand=True, fill=True)
        if self.getConfigValue("add_shortcut_bar"):
            shortcutBar = scriptEngine.createShortcutBar()
            vbox.pack_start(shortcutBar, expand=False, fill=False)
            shortcutBar.show()

        if self.getConfigValue("add_status_bar"):
            vbox.pack_start(self.status.createStatusbar(), expand=False, fill=False)
        vbox.show()
        topWindow.add(vbox)
        topWindow.show()        
        if (self.dynamic and self.getConfigValue("window_size").has_key("dynamic_maximize") and self.getConfigValue("window_size")["dynamic_maximize"][0] == "1") or (not self.dynamic and self.getConfigValue("window_size").has_key("static_maximize") and self.getConfigValue("window_size")["static_maximize"][0] == "1"):
            topWindow.maximize()
        else:
            width = self.getWindowWidth()
            height = self.getWindowHeight()
            topWindow.resize(width, height)
        self.rightWindowGUI.notifySizeChange(topWindow.get_size()[0], topWindow.get_size()[1], self.getConfigValue("window_size"))
        verticalSeparatorPosition = 0.5
        if self.dynamic and self.getConfigValue("window_size").has_key("dynamic_vertical_separator_position"):
            verticalSeparatorPosition = float(self.getConfigValue("window_size")["dynamic_vertical_separator_position"][0])
        elif not self.dynamic and self.getConfigValue("window_size").has_key("static_vertical_separator_position"):
            verticalSeparatorPosition = float(self.getConfigValue("window_size")["static_vertical_separator_position"][0])
        self.contents.set_position(int(self.contents.allocation.width * verticalSeparatorPosition))
    def placeTopWidgets(self, vbox):
        # Initialize
        self.uiManager.add_ui_from_string(self.defaultGUIDescription)
        self.selectionActionGUI.attachTriggers()
  
        # Show menu/toolbar?
        menubar = None
        toolbar = None
        if (self.dynamic and self.getConfigValue("dynamic_gui_show_menubar")) or (not self.dynamic and self.getConfigValue("static_gui_show_menubar")):
            menubar = self.uiManager.get_widget("/menubar")
        if (self.dynamic and self.getConfigValue("dynamic_gui_show_toolbar")) or (not self.dynamic and self.getConfigValue("static_gui_show_toolbar")):
            toolbarHandle = gtk.HandleBox()
            toolbar = self.uiManager.get_widget("/toolbar")
            toolbarHandle.add(toolbar)
            for item in toolbar.get_children():
                item.set_is_important(True)
                toolbar.set_orientation(gtk.ORIENTATION_HORIZONTAL)
                toolbar.set_style(gtk.TOOLBAR_BOTH_HORIZ)
        
        progressBar = None
        if self.dynamic:            
            progressBar = self.progressBar.createProgressBar()
            progressBar.show()
        hbox = gtk.HBox()

        if menubar and toolbar:
            vbox.pack_start(menubar, expand=False, fill=False)
            hbox.pack_start(toolbarHandle, expand=True, fill=True)
        elif menubar:
            hbox.pack_start(menubar, expand=False, fill=False)
        elif toolbar:
            hbox.pack_start(toolbarHandle, expand=True, fill=True)

        if progressBar:
            if toolbar:
                width = 7 # Looks good, same as gtk.Paned border width
            else:
                width = 0
            alignment = gtk.Alignment()
            alignment.set_padding(width, width, 1, width)
            alignment.add(progressBar)
            if toolbar:
                toolItem = gtk.ToolItem()
                toolItem.add(alignment)
                toolItem.set_expand(True)
                toolbar.insert(toolItem, -1)
            else:
                hbox.pack_start(alignment, expand=True, fill=True)

        hbox.show_all()
        vbox.pack_start(hbox, expand=False, fill=True)
    def getConfigValue(self, configName):
        return self.rootSuites[0].app.getConfigValue(configName)
    def getWindowHeight(self):
        defaultHeight = (gtk.gdk.screen_height() * 5) / 6
        height = defaultHeight

        windowSizeOptions = self.getConfigValue("window_size")
        if not self.dynamic:
            if windowSizeOptions.has_key("static_height_pixels"):
                height = int(windowSizeOptions["static_height_pixels"][0])
            if windowSizeOptions.has_key("static_height_screen"):
                height = gtk.gdk.screen_height() * float(windowSizeOptions["static_height_screen"][0])
        else:
            if windowSizeOptions.has_key("dynamic_height_pixels"):
                height = int(windowSizeOptions["dynamic_height_pixels"][0])
            if windowSizeOptions.has_key("dynamic_height_screen"):
                height = gtk.gdk.screen_height() * float(windowSizeOptions["dynamic_height_screen"][0])                

        return int(height)
    def getWindowWidth(self):
        if self.dynamic:
            defaultWidth = gtk.gdk.screen_width() * 0.5
        else:
            defaultWidth = gtk.gdk.screen_width() * 0.6
        width = defaultWidth        

        windowSizeOptions = self.getConfigValue("window_size")
        if not self.dynamic:
            if windowSizeOptions.has_key("static_width_pixels"):
                width = int(windowSizeOptions["static_width_pixels"][0])
            if windowSizeOptions.has_key("static_width_screen"):
                width = gtk.gdk.screen_width() * float(windowSizeOptions["static_width_screen"][0])
        else:
            if windowSizeOptions.has_key("dynamic_width_pixels"):
                width = int(windowSizeOptions["dynamic_width_pixels"][0])
            if windowSizeOptions.has_key("dynamic_width_screen"):
                width = gtk.gdk.screen_width() * float(windowSizeOptions["dynamic_width_screen"][0])                

        return int(width)
    def addSuite(self, suite):
        self.rootSuites.append(suite)
        if not suite.app.getConfigValue("add_shortcut_bar"):
            scriptEngine.enableShortcuts = 0
        self.testTreeGUI.addSuite(suite)        
    def createWindowContents(self, testWins, testCaseWin):
        self.contents = gtk.HPaned()
        self.contents.connect('notify', self.paneHasChanged)
        self.contents.pack1(testWins, resize=True)
        self.contents.pack2(testCaseWin, resize=True)
        self.contents.show()
        return self.contents
    def paneHasChanged(self, pane, gparamspec):
        pos = pane.get_position()
        size = pane.allocation.width
        self.toolTips.set_tip(pane, "Position: " + str(pos) + "/" + str(size) + " (" + str(100 * pos / size) + "% from the left edge)")
    def createTestWindows(self, treeWindow):
        # Create a vertical box to hold the above stuff.
        vbox = gtk.VBox()
        vbox.pack_start(treeWindow, expand=True, fill=True)
        vbox.show()
        return vbox
    def createTreeWindow(self):
        treeView = self.testTreeGUI.makeTreeView()
        # Create scrollbars around the view.
        scrolled = gtk.ScrolledWindow()
        scrolled.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        scrolled.add(treeView)
        framed = gtk.Frame()
        framed.set_shadow_type(gtk.SHADOW_IN)
        framed.add(scrolled)        
        framed.show_all()
        return framed
    def createSelectionActionGUI(self, topWindow, actionThread):
        actions = [ QuitGUI(self.rootSuites, self.dynamic, topWindow, actionThread) ]
        actions += guiplugins.interactiveActionHandler.getSelectionInstances(self.rootSuites, self.dynamic)
        for action in actions:
            # These actions might change the tree view selection or the status bar, need to observe them
            action.addObserver(self.testTreeGUI)
            action.addObserver(self.status)
        selActionGUI = SelectionActionGUI(actions, self.uiManager, self.rootSuites[0].app)
        # selection actions need to observer for manual selections
        self.testTreeGUI.addObserver(selActionGUI)
        return selActionGUI
    def setUpGui(self, actionThread=None):
        topWindow = self.createTopWindow()
        treeWindow = self.createTreeWindow()
        self.selectionActionGUI = self.createSelectionActionGUI(topWindow, actionThread)
        testWins = self.createTestWindows(treeWindow)

        # Must be created after addSuiteWithParents has counted all tests ...
        # (but before RightWindowGUI, as that wants in on progress)
        if self.dynamic:
            self.progressBar = TestProgressBar(self.testTreeGUI.totalNofTests)
            self.progressMonitor = TestProgressMonitor()
            self.progressMonitor.addObserver(self.testTreeGUI)
            
        self.rightWindowGUI = self.createDefaultRightGUI()
        # watch for double-clicks
        self.testTreeGUI.addObserver(self.rightWindowGUI)
        self.fillTopWindow(topWindow, testWins, self.rightWindowGUI.getWindow())
    def runWithActionThread(self, actionThread):
        plugins.Observable.threadedNotificationHandler.enablePoll(gobject.idle_add)
        self.setUpGui(actionThread)
        actionThread.start()
        gtk.main()
    def runAlone(self):
        self.setUpGui()
        gobject.idle_add(self.pickUpProcess)
        gtk.main()
    def createDefaultRightGUI(self):
        rootSuite = self.rootSuites[0]
        guilog.info("Viewing test " + repr(rootSuite))
        return RightWindowGUI(rootSuite, self.dynamic, self.selectionActionGUI, self.progressMonitor, self.uiManager)
    def pickUpProcess(self):
        process = guiplugins.processTerminationMonitor.getTerminatedProcess()
        if process:
            try:
                process.runExitHandler()
            except plugins.TextTestError, e:
                showError(str(e))
        
        # We must sleep for a bit, or we use the whole CPU (busy-wait)
        time.sleep(0.1)
        return True
    def notifyLifecycleChange(self, test, state, changeDesc):
        # Working around python bug 853411: main thread must do all forking
        state.notifyInMainThread()
        
        self.testTreeGUI.notifyLifecycleChange(test, state, changeDesc)
        self.rightWindowGUI.notifyLifecycleChange(test, state, changeDesc)
        if self.progressBar:
            self.progressBar.notifyLifecycleChange(test, state, changeDesc)
        if self.progressMonitor:
            self.progressMonitor.notifyLifecycleChange(test, state, changeDesc)
    def notifyFileChange(self, test):
        self.rightWindowGUI.notifyFileChange(test)
    def notifyContentChange(self, suite):
        self.testTreeGUI.notifyContentChange(suite)
    def notifyAdd(self, test):
        self.testTreeGUI.notifyAdd(test)
        self.rightWindowGUI.notifyAdd(test)
    def notifyRemove(self, test):
        self.testTreeGUI.notifyRemove(test)
        self.rightWindowGUI.notifyRemove(test)
    def notifyAllComplete(self):
        plugins.Observable.threadedNotificationHandler.disablePoll()
            
class TestTreeGUI(plugins.Observable):
    def __init__(self, dynamic):
        plugins.Observable.__init__(self)
        self.model = gtk.TreeStore(gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_PYOBJECT,\
                                   gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_BOOLEAN)
        self.itermap = seqdict()
        self.selection = None
        self.dynamic = dynamic
        self.totalNofTests = 0
        self.collapseStatic = False
        self.successPerSuite = {} # map from suite to number succeeded
        self.collapsedRows = {}
    def addApplication(self, app):
        colour = app.getConfigValue("test_colours")["app_static"]
        iter = self.model.insert_before(None, None)
        nodeName = "Application " + app.fullName
        self.model.set_value(iter, 0, nodeName)
        self.model.set_value(iter, 1, colour)
        self.model.set_value(iter, 2, app)
        self.model.set_value(iter, 3, nodeName)
        self.model.set_value(iter, 6, True)
        self.collapseStatic = app.getConfigValue("static_collapse_suites")
    def addSuite(self, suite):
        if not self.dynamic:
            self.addApplication(suite.app)
        size = suite.size()
        self.totalNofTests += size
        if not self.dynamic or size > 0:
            self.addSuiteWithParent(suite, None)
    def visibleByDefault(self, suite, parent):
        if parent == None or not self.dynamic:
            return True
        hideCategories = suite.getConfigValue("hide_test_category")
        return "non_started" not in hideCategories
    def addSuiteWithParent(self, suite, parent):    
        iter = self.model.insert_before(parent, None)
        nodeName = suite.name
        if parent == None:
            appName = suite.app.name + suite.app.versionSuffix()
            if appName != nodeName:
                nodeName += " (" + appName + ")"
        self.model.set_value(iter, 0, nodeName)
        self.model.set_value(iter, 2, suite)
        self.model.set_value(iter, 3, suite.uniqueName)
        self.model.set_value(iter, 6, self.visibleByDefault(suite, parent))
        if suite.classId() != "test-app":
            storeIter = iter.copy()
            self.itermap[suite] = storeIter
        self.updateStateInModel(suite, iter, suite.state)
        if suite.classId() == "test-suite":
            for test in suite.testcases:
                self.addSuiteWithParent(test, iter)
        return iter
    def updateStateInModel(self, test, iter, state):
        if not self.dynamic:
            return self.modelUpdate(iter, getTestColour(test, "static"))

        resultType, summary = state.getTypeBreakdown()
        return self.modelUpdate(iter, getTestColour(test, resultType), summary, getTestColour(test, state.category))
    def modelUpdate(self, iter, colour, details="", colour2=None):
        if not colour2:
            colour2 = colour
        self.model.set_value(iter, 1, colour)
        if self.dynamic:
            self.model.set_value(iter, 4, details)
            self.model.set_value(iter, 5, colour2)
    def makeTreeView(self):
        self.filteredModel = self.model.filter_new()
        # It seems that TreeModelFilter might not like new
        # rows being added to the original model - the AddUsers
        # test crashed/produced a gtk warning before I added
        # this if statement (for the dynamic GUI we never add rows)
        if self.dynamic:
            self.filteredModel.set_visible_column(6)
        self.treeView = gtk.TreeView(self.filteredModel)
        self.selection = self.treeView.get_selection()
        self.selection.set_mode(gtk.SELECTION_MULTIPLE)
        self.selection.connect("changed", self.selectionChanged)
        testRenderer = gtk.CellRendererText()
        testsColumnTitle = "Tests: 0/" + str(self.totalNofTests) + " selected"
        if self.dynamic:
            testsColumnTitle = "Tests: 0/" + str(self.totalNofTests) + " selected, all visible"
        self.testsColumn = gtk.TreeViewColumn(testsColumnTitle, testRenderer, text=0, background=1)
        self.testsColumn.set_cell_data_func(testRenderer, renderSuitesBold)
        self.treeView.append_column(self.testsColumn)
        if self.dynamic:
            detailsRenderer = gtk.CellRendererText()
            perfColumn = gtk.TreeViewColumn("Details", detailsRenderer, text=4, background=5)
            self.treeView.append_column(perfColumn)

        modelIndexer = TreeModelIndexer(self.filteredModel, self.testsColumn, 3)
        scriptEngine.monitorExpansion(self.treeView, "show test suite", "hide test suite", modelIndexer)
        self.treeView.connect('row-expanded', self.rowExpanded)
        guilog.info("Expanding tests in tree view...")
        self.expandLevel(self.treeView, self.filteredModel.get_iter_root())
        guilog.info("")
        
        # The order of these two is vital!
        scriptEngine.connect("select test", "row_activated", self.treeView, self.viewTest, modelIndexer)
        scriptEngine.monitor("set test selection to", self.selection, modelIndexer)
        self.treeView.show()
        if self.dynamic:
            self.filteredModel.connect('row-inserted', self.rowInserted)
            self.reFilter()
        self.treeView.grab_focus() # to avoid Quit button getting initial focus
        return self.treeView
    def rowCollapsed(self, treeview, iter, path):
        if self.dynamic:
            realPath = self.filteredModel.convert_path_to_child_path(path)
            self.collapsedRows[realPath] = 1
    def rowExpanded(self, treeview, iter, path):
        if self.dynamic:
            realPath = self.filteredModel.convert_path_to_child_path(path)
            if self.collapsedRows.has_key(realPath):
                del self.collapsedRows[realPath]
        self.expandLevel(treeview, self.filteredModel.iter_children(iter), not self.collapseStatic)
    def rowInserted(self, model, path, iter):
        realPath = self.filteredModel.convert_path_to_child_path(path)
        self.expandRow(self.filteredModel.iter_parent(iter), False)
        self.selectionChanged(self.selection, False)
    def expandRow(self, iter, recurse):
        if iter == None:
            return
        path = self.filteredModel.get_path(iter)
        realPath = self.filteredModel.convert_path_to_child_path(path)
        
        # Iterate over children, call self if they have children
        if not self.collapsedRows.has_key(realPath):
            self.treeView.expand_row(path, open_all=False)
        if recurse:
            childIter = self.filteredModel.iter_children(iter)
            while (childIter != None):
                if self.filteredModel.iter_has_child(childIter):
                    self.expandRow(childIter, True)
                childIter = self.filteredModel.iter_next(childIter)
                
    def collapseRow(self, iter):
        # To make sure that the path is marked as 'collapsed' even if the row cannot be collapsed
        # (if the suite is empty, or not shown at all), we set self.collapsedRow manually, instead of
        # waiting for rowCollapsed() to do it at the 'row-collapsed' signal (which will not be emitted
        # in the above cases)
        path = self.model.get_path(iter)
        self.collapsedRows[path] = 1
        try:
            filterPath = self.filteredModel.convert_child_path_to_path(path)
            self.selection.get_tree_view().collapse_row(filterPath)
        except:
            pass

    def selectionChanged(self, selection, printToLog = True):
        self.totalNofTestsShown = 0

        allSelected, selectedTests = self.getSelected()
        self.nofSelectedTests = len(selectedTests)
        self.notify("NewTestSelection", allSelected)
        self.filteredModel.foreach(self.countVisible)
        self.updateColumnTitle(printToLog)
    def updateColumnTitle(self, printToLog=True):
        title = "Tests: "
        if self.nofSelectedTests == self.totalNofTests:
            title += "All " + str(self.totalNofTests) + " selected"
        else:
            title += str(self.nofSelectedTests) + "/" + str(self.totalNofTests) + " selected"
        if self.dynamic:
            if self.totalNofTestsShown == self.totalNofTests:
                title += ", all visible"
            else:
                title += ", " + str(self.totalNofTestsShown) + " visible"
        self.testsColumn.set_title(title)
        if printToLog:
            guilog.info(title)
    def getSelected(self):
        # add self as an observer
        allSelected, selectedTests = [], []
        self.selection.selected_foreach(self.addSelTest, (allSelected, selectedTests))
        return allSelected, selectedTests
    def addSelTest(self, model, path, iter, lists, *args):
        test = model.get_value(iter, 2)
        allSelected, selectedTests = lists
        allSelected.append(test)
        if test.classId() == "test-case":
            selectedTests.append(test)
    def findIter(self, test):
        try:
            return self.filteredModel.convert_child_iter_to_iter(self.itermap[test])
        except RuntimeError:
            pass # convert_child_iter_to_iter throws RunTimeError if the row is hidden in the TreeModelFilter
    def notifyNewTestSelection(self, selTests, selectCollapsed=True):
        self.selection.unselect_all()
        firstPath = None
        for test in selTests:
            iter = self.findIter(test)
            if not iter:
                continue
            path = self.filteredModel.get_path(iter) 
            if not firstPath:
                firstPath = path
            if selectCollapsed:
                self.selection.get_tree_view().expand_to_path(path)
            self.selection.select_iter(iter)
        self.selection.get_tree_view().grab_focus()
        if firstPath is not None:
            self.selection.get_tree_view().scroll_to_cell(firstPath, None, True, 0.1)
        guilog.info("Marking " + str(self.selection.count_selected_rows()) + " tests as selected")
    def countVisible(self, model, path, iter):
        # When rows are added, they are first empty, and asking for
        # classId on NoneType gives an error. See e.g.
        # http://www.async.com.br/faq/pygtk/index.py?req=show&file=faq13.028.htp
        if model.get_value(iter, 2) == None:
            return
        if model.get_value(iter, 2).classId() == "test-case":
            self.totalNofTestsShown = self.totalNofTestsShown + 1
    def expandLevel(self, view, iter, recursive=True):
        # Make sure expanding expands everything, better than just one level as default...
        # Avoid using view.expand_row(path, open_all=True), as the open_all flag
        # doesn't seem to send the correct 'row-expanded' signal for all rows ...
        # This way, the signals are generated one at a time and we call back into here.
        model = view.get_model()
        while (iter != None):
            test = model.get_value(iter, 2)
            guilog.info("-> " + test.getIndent() + "Added " + repr(test) + " to test tree view.")
            if recursive:
                view.expand_row(model.get_path(iter), open_all=False)
             
            iter = view.get_model().iter_next(iter)
    def notifyLifecycleChange(self, test, state, changeDesc):
        iter = self.itermap[test]
        self.updateStateInModel(test, iter, state)
        self.diagnoseTest(test, iter)

        if state.hasSucceeded():
            self.updateSuiteSuccess(test.parent)
    def updateSuiteSuccess(self, suite):
        successCount = self.successPerSuite.get(suite, 0) + 1
        self.successPerSuite[suite] = successCount
        suiteSize = suite.size()
        if successCount == suiteSize:
            self.setAllSucceeded(suite, suiteSize)

        if suite.parent:
            self.updateSuiteSuccess(suite.parent)
            
    def diagnoseTest(self, test, iter):
        guilog.info("Redrawing test " + test.name + " coloured " + self.model.get_value(iter, 1))
        secondColumnText = self.model.get_value(iter, 4)
        if self.dynamic and secondColumnText:
            guilog.info("(Second column '" + secondColumnText + "' coloured " + self.model.get_value(iter, 5) + ")")
    def setAllSucceeded(self, suite, suiteSize):
        # Print how many tests succeeded, color details column in success color,
        # collapse row, and try to collapse parent suite.
        detailText = "All " + str(suiteSize) + " tests successful"
        successColour = getTestColour(suite, "success")
        iter = self.itermap[suite]
        self.model.set_value(iter, 4, detailText)
        self.model.set_value(iter, 5, successColour)
        guilog.info("Redrawing suite " + suite.name + " : second column '" + detailText +  "' coloured " + successColour)

        if suite.getConfigValue("auto_collapse_successful") == 1:
            self.collapseRow(iter)
            
    def notifyAdd(self, test):
        self.addTest(test)
        if test.classId() == "test-case":
            self.totalNofTests += 1
        self.notifyNewTestSelection([ test ])
    def addTest(self, test):
        suiteIter = self.itermap[test.parent]
        iter = self.addSuiteWithParent(test, suiteIter)
    def notifyRemove(self, test):
        self.removeTest(test)
        if test.classId() == "test-case":
            self.totalNofTests -= 1
            self.updateColumnTitle()
    def removeTest(self, test):
        guilog.info("-> " + test.getIndent() + "Removed " + repr(test) + " from test tree view.")
        iter = self.itermap[test]
        filteredIter = self.findIter(test)
        if self.selection.iter_is_selected(filteredIter):
            self.selection.unselect_iter(filteredIter)
        self.model.remove(iter)
        del self.itermap[test]
    def notifyContentChange(self, suite):
        allSelected, selectedTests = self.getSelected()
        self.selection.unselect_all()
        guilog.info("-> " + suite.getIndent() + "Recreating contents of " + repr(suite) + ".")
        for test in suite.testcases:
            self.removeTest(test)
        for test in suite.testcases:
            self.addTest(test)
        self.notifyNewTestSelection(allSelected)
    def viewTest(self, view, path, column, *args):
        iter = self.filteredModel.get_iter(path)
        self.selection.select_iter(iter)
        self.viewTestAtIter(iter)
    def viewTestAtIter(self, iter):
        test = self.filteredModel.get_value(iter, 2)
        guilog.info("Viewing test " + repr(test))
        if test.classId() == "test-case":
            self.checkUpToDate(test)
        self.notify("ViewTest", test)
    def checkUpToDate(self, test):
        if test.state.isComplete() and test.state.needsRecalculation():
            cmpAction = comparetest.MakeComparisons()
            guilog.info("Recalculating result info for test: result file changed since created")
            cmpAction(test)
            test.notify("LifecycleChange", test.state, "be recalculated")
    def notifyVisibility(self, test, newValue):
        allIterators = self.findVisibilityIterators(test) # returns leaf-to-root order, good for hiding
        if newValue:
            allIterators.reverse()  # but when showing, we want to go root-to-leaf

        for iterator in allIterators:
            if newValue or not self.hasVisibleChildren(iterator):
                self.setVisibility(iterator, newValue)
        
        self.reFilter()

    def setVisibility(self, iter, newValue):
        oldValue = self.model.get_value(iter, 6)
        if oldValue == newValue:
            return

        test = self.model.get_value(iter, 2)
        if newValue:
            guilog.info("Making test visible : " + repr(test))
        else:
            guilog.info("Hiding test : " + repr(test))
        self.model.set_value(iter, 6, newValue)
        
    def findVisibilityIterators(self, test):
        iter = self.itermap[test]
        parents = []
        parent = self.model.iter_parent(iter)
        while parent != None:
            parents.append(parent)                    
            parent = self.model.iter_parent(parent)
        # Don't include the root which we never hide
        return [ iter ] + parents[:-1]

    def hasVisibleChildren(self, iter):
        child = self.model.iter_children(iter)
        while (child != None):
            if self.model.get_value(child, 6):
                return True
            else:
                child = self.model.iter_next(child)
        return False
    
    def reFilter(self):
        self.filteredModel.refilter()
        self.selectionChanged(self.selection, False)
        rootIter = self.filteredModel.get_iter_root()
        while rootIter != None:
            self.expandRow(rootIter, True)
            rootIter = self.filteredModel.iter_next(rootIter)
   
class InteractiveActionGUI:
    def __init__(self, actions, uiManager, app):
        self.app = app
        self.uiManager = uiManager
        self.actions = actions
        self.pageDescInfo = { "Test" : {} }
        self.indexers = [] # Utility list for getting the values from multi-valued radio button groups :-(
        self.createdActions = []
    def getInterfaceDescription(self):
        description = "<ui>\n"
        buttonInstances = filter(lambda instance : instance.inToolBar(), self.actions)
        for instance in buttonInstances:
            description += instance.getInterfaceDescription()
        description += "</ui>"
        return description
    def attachTriggers(self):
        self.makeActions()
        self.mergeId = self.uiManager.add_ui_from_string(self.getInterfaceDescription())
        self.uiManager.ensure_update()
        toolbar = self.uiManager.get_widget("/toolbar")
        if toolbar:
            for item in toolbar.get_children(): 
                item.set_is_important(True) # Or newly added children without stock ids won't be visible in gtk.TOOLBAR_BOTH_HORIZ style
    def detachTriggers(self):
        self.disconnectAccelerators()
        self.uiManager.remove_ui(self.mergeId)
        self.uiManager.ensure_update()
    def makeActions(self):
        actions = filter(lambda instance : instance.inToolBar(), self.actions)
        for action in actions:
            self.createGtkAction(action)
    def createGtkAction(self, intvAction, tab=False):
        actionName = intvAction.getSecondaryTitle()
        label = intvAction.getTitle()
        stockId = intvAction.getStockId()
        if stockId:
            stockId = "gtk-" + stockId 
        gtkAction = gtk.Action(actionName, label, intvAction.getTooltip(), stockId)
        realAcc = intvAction.getAccelerator()
        realAcc = self.getCustomAccelerator(actionName, label, realAcc)
        if realAcc:
            key, mod = gtk.accelerator_parse(realAcc)
            if not gtk.accelerator_valid(key, mod):
                print "Warning: Keyboard accelerator '" + realAcc + "' for action '" + actionName + "' is not valid, ignoring ..."
                realAcc = None
                
        guilog.info("Creating action '" + actionName + "' with label '" + repr(label) + \
                    "', stock id '" + repr(stockId) + "' and accelerator " + repr(realAcc))
        self.getActionGroup().add_action_with_accel(gtkAction, realAcc)
        gtkAction.set_accel_group(self.uiManager.get_accel_group())
        gtkAction.connect_accelerator()
        scriptTitle = intvAction.getScriptTitle(tab).replace("_", "")
        scriptEngine.connect(scriptTitle, "activate", gtkAction, self.runInteractive, None, intvAction)
        self.createdActions.append(gtkAction)
        return gtkAction
    def makeButtons(self):
        executeButtons = gtk.HBox()
        buttonInstances = filter(lambda instance : instance.inToolBar(), self.actions)
        for instance in buttonInstances:
            button = self.createButton(instance)
            executeButtons.pack_start(button, expand=False, fill=False)
        if len(buttonInstances) > 0:
            buttonTitles = map(lambda b: b.getTitle(), buttonInstances)
            guilog.info("Creating box with buttons : " + string.join(buttonTitles, ", "))
        executeButtons.show_all()
        return executeButtons
    def createButton(self, intvAction, tab=False):
        action = self.createGtkAction(intvAction, tab)
        button = gtk.Button()
        action.connect_proxy(button)
        button.show()
        return button
    def getActionGroup(self):
        return self.uiManager.get_action_groups()[self.getActionGroupIndex()]
    def getCustomAccelerator(self, name, label, original):
        configName = label.replace("_", "").replace(" ", "_").lower()
        if self.app.getConfigValue("gui_accelerators").has_key(configName):
            newAccel = self.app.getConfigValue("gui_accelerators")[configName][0]
            guilog.info("Replacing default accelerator '" + repr(original) + "' for action '" + name + "' by config value '" + newAccel + "'")
            return newAccel
        return original
    def disconnectAccelerators(self):
        for action in self.getActionGroup().list_actions():
            guilog.info("Disconnecting accelerator for action '" + action.get_name() + "'")
            action.disconnect_accelerator()
    def runInteractive(self, button, action, *args):
        doubleCheckMessage = action.getDoubleCheckMessage(self.getTestObject())
        if doubleCheckMessage:
            self.dialog = DoubleCheckDialog(doubleCheckMessage, self._runInteractive, (action,))
        else:
            self._runInteractive(action)
    def _runInteractive(self, action):
        try:
            action.perform(self.getTestObject(), self.getFileObject())
        except plugins.TextTestError, e:
            showError(str(e))
    def getPageDescription(self, pageName, subPageName = ""):
        info = self.getPageDescInfo(pageName, subPageName)
        if info is None:
            return
        optionGroup, buttonDesc = info
        return "Viewing notebook page for '" + optionGroup.name + "'\n" + \
               self.describeOptionGroup(optionGroup) + buttonDesc                    
    def getPageDescInfo(self, pageName, subPageName):
        if subPageName:
            return self.pageDescInfo.get(pageName).get(subPageName)
        else:
            return self.pageDescInfo.get("Test").get(pageName)
    def createOptionGroupPages(self):
        pages = seqdict()
        pages["Test"] = []
        for instance in self.actions:
            instanceTab = instance.getGroupTabTitle()
            optionGroups = instance.getOptionGroups()
            hasButton = len(optionGroups) == 1 and instance.canPerform()
            for optionGroup in optionGroups:
                if optionGroup.switches or optionGroup.options:
                    display = self.createDisplay(optionGroup, hasButton, instance)
                    buttonDesc = self.describeButton(hasButton, instance)
                    if not pages.has_key(instanceTab):
                        pages[instanceTab] = []
                        self.pageDescInfo[instanceTab] = {}
                    self.pageDescInfo[instanceTab][optionGroup.name] = optionGroup, buttonDesc
                    pages[instanceTab].append((display, optionGroup.name))
        return pages
    def createDisplay(self, optionGroup, hasButton, instance):
        vbox = gtk.VBox()
        if len(optionGroup.options) > 0:
            # Creating 0-row table gives a warning ...
            table = gtk.Table(len(optionGroup.options), 2, homogeneous=False)
            table.set_row_spacings(1)
            rowIndex = 0        
            for option in optionGroup.options.values():
                label, entry = self.createOptionEntry(option)
                label.set_alignment(1.0, 0.5)
                table.attach(label, 0, 1, rowIndex, rowIndex + 1, xoptions=gtk.FILL, xpadding=1)
                table.attach(entry, 1, 2, rowIndex, rowIndex + 1)
                rowIndex += 1
                table.show_all()
            vbox.pack_start(table, expand=False, fill=False)
        
        for switch in optionGroup.switches.values():
            hbox = self.createSwitchBox(switch)
            vbox.pack_start(hbox, expand=False, fill=False)
        if hasButton:
            button = self.createButton(instance, tab=True)
            buttonbox = gtk.HBox()
            buttonbox.pack_start(button, expand=True, fill=False)
            buttonbox.show()
            vbox.pack_start(buttonbox, expand=False, fill=False, padding=8)
        vbox.show()
        return vbox
    def describeOptionGroup(self, optionGroup):
        displayDesc = ""
        for option in optionGroup.options.values():
            displayDesc += self.diagnoseOption(option) + "\n"
        for switch in optionGroup.switches.values():
            displayDesc += self.diagnoseSwitch(switch) + "\n"
        return displayDesc
    def describeButton(self, hasButton, instance):
        if hasButton:
            return "Viewing button with title '" + instance.getTitle() + "'"
        else:
            return ""
    def createComboBox(self, option):
        combobox = gtk.combo_box_entry_new_text()
        entry = combobox.child
        option.setPossibleValuesAppendMethod(combobox.append_text)
        return combobox, entry
    def createOptionWidget(self, option):
        if len(option.possibleValues) > 1:
            return self.createComboBox(option)
        else:
            entry = gtk.Entry()
            return entry, entry
    def createOptionEntry(self, option):
        label = gtk.Label(option.name + "  ")
        widget, entry = self.createOptionWidget(option)
        entry.set_text(option.getValue())
        scriptEngine.registerEntry(entry, "enter " + option.name + " =")
        option.setMethods(entry.get_text, entry.set_text)
        return label, widget
    def createSwitchBox(self, switch):
        self.diagnoseSwitch(switch)
        if len(switch.options) >= 1:
            hbox = gtk.HBox()
            hbox.pack_start(gtk.Label(switch.name), expand=False, fill=False)
            count = 0
            buttons = []
            mainRadioButton = None
            for option in switch.options:
                radioButton = gtk.RadioButton(mainRadioButton, option)
                buttons.append(radioButton)
                scriptEngine.registerToggleButton(radioButton, "choose " + option)
                if not mainRadioButton:
                    mainRadioButton = radioButton
                if count == switch.getValue():
                    radioButton.set_active(True)
                    switch.resetMethod = radioButton.set_active
                else:
                    radioButton.set_active(False)
                hbox.pack_start(radioButton, expand=True, fill=True)
                count = count + 1
            indexer = RadioGroupIndexer(buttons)
            self.indexers.append(indexer)
            switch.setMethods(indexer.getActiveIndex, indexer.setActiveIndex)
            hbox.show_all()
            return hbox  
        else:
            checkButton = gtk.CheckButton(switch.name)
            if switch.getValue():
                checkButton.set_active(True)
            scriptEngine.registerToggleButton(checkButton, "check " + switch.name, "uncheck " + switch.name)
            switch.setMethods(checkButton.get_active, checkButton.set_active)
            checkButton.show()
            return checkButton
    def diagnoseOption(self, option):
        value = option.getValue()
        text = "Viewing entry for option '" + option.name + "'"
        if len(value) > 0:
            text += " (set to '" + value + "')"
        if len(option.possibleValues) > 1:
            text += " (drop-down list containing " + repr(option.possibleValues) + ")"
        return text
    def diagnoseSwitch(self, switch):
        value = switch.getValue()
        if len(switch.options) >= 1:
            text = "Viewing radio button for switch '" + switch.name + "', options "
            text += string.join(switch.options, "/")
            text += "'. Default value " + str(value) + "."
        else:
            text = "Viewing check button for switch '" + switch.name + "'"
            if value:
                text += " (checked)"
        return text

class TestActionGUI(InteractiveActionGUI):
    def __init__(self, actions, uiManager, app, test):
        InteractiveActionGUI.__init__(self, actions, uiManager, app)
        self.test = test
    def getTestObject(self):
        return self.test
    def getFileObject(self):
        return []
    def getActionGroupIndex(self):
        if self.test.classId() == "test-suite":
            return 1
        else:
            return 2    
        
class SelectionActionGUI(InteractiveActionGUI):
    def __init__(self, actions, uiManager, app):
        InteractiveActionGUI.__init__(self, actions, uiManager, app)
        self.currTestSelection = []
        self.currFileSelection = []
    def notifyNewTestSelection(self, tests):
        self.currTestSelection = tests
    def notifyNewFileSelection(self, fileSel):
        self.currFileSelection = fileSel
    def getTestObject(self):
        return self.currTestSelection
    def getFileObject(self):
        return self.currFileSelection
    def getActionGroupIndex(self):
        return 0
            
class RightWindowGUI:
    def __init__(self, object, dynamic, selectionActionGUI, progressMonitor, uiManager):
        self.dynamic = dynamic
        self.intvActionGUI = None
        self.uiManager = uiManager
        self.selectionActionGUI = selectionActionGUI
        self.progressMonitor = progressMonitor
        self.window = gtk.VBox()
        self.vpaned = gtk.VPaned()
        self.vpaned.connect('notify', self.paneHasChanged)
        self.panedTooltips = gtk.Tooltips()
        self.topFrame = gtk.Frame()
        self.topFrame.set_shadow_type(gtk.SHADOW_IN)
        self.bottomFrame = gtk.Frame()
        self.bottomFrame.set_shadow_type(gtk.SHADOW_IN)
        self.vpaned.pack1(self.topFrame, resize=True)
        self.vpaned.pack2(self.bottomFrame, resize=True)
        self.currentObject = object
        buttonBar, fileView, objectPages = self.makeObjectDependentContents(object)
        self.diag = plugins.getDiagnostics("GUI notebook")
        self.notebook = self.createNotebook(objectPages, self.selectionActionGUI)
        self.oldObjectPageNames = self.makeDictionary(objectPages).keys()
        self.describeNotebook(self.notebook, pageNum=0)
        self.bottomFrame.add(self.notebook)
        self.fillWindow(buttonBar, fileView)
    def paneHasChanged(self, pane, gparamspec):
        pos = pane.get_position()
        size = pane.allocation.height
        self.panedTooltips.set_tip(pane, "Position: " + str(pos) + "/" + str(size) + " (" + str(100 * pos / size) + "% from the top)")
    def makeDictionary(self, objectPages):
        dict = seqdict()
        for page, name in objectPages:
            dict[name] = page
        return dict
    def notifySizeChange(self, width, height, options):
        horizontalSeparatorPosition = 0.46
        if self.dynamic and options.has_key("dynamic_horizontal_separator_position"):
            horizontalSeparatorPosition = float(options["dynamic_horizontal_separator_position"][0])
        elif not self.dynamic and options.has_key("static_horizontal_separator_position"):
            horizontalSeparatorPosition = float(options["static_horizontal_separator_position"][0])

        self.vpaned.set_position(int(self.vpaned.allocation.height * horizontalSeparatorPosition))        
    def notifyFileChange(self, object):
        # Test has changed contents or state, regenerate if we're currently viewing it
        if self.currentObject is object:
            self.view(object, resetNotebook=False)
    def notifyRemove(self, object):
        # If we're viewing a test that isn't there any more, view the suite (its parent) instead!
        if self.currentObject is object:
            self.notifyViewTest(object.parent)
    def notifyLifecycleChange(self, test, state, changeDesc):
        self.notifyFileChange(test)
    def notifyViewTest(self, test):
        # Triggered by user double-clicking the test in the test tree
        self.view(test, resetNotebook=True)
    def notifyAdd(self, test):
        guilog.info("Viewing new test " + test.name)
        self.notifyViewTest(test)
    def view(self, object, resetNotebook):
        for child in self.window.get_children():
            if not child is self.notebook:
                self.window.remove(child)
        if not self.topFrame.get_child() is self.notebook:
            self.topFrame.remove(self.topFrame.get_child())
        if not self.bottomFrame.get_child() is self.notebook:
            self.bottomFrame.remove(self.bottomFrame.get_child())
        self.currentObject = object
        buttonBar, fileView, objectPages = self.makeObjectDependentContents(object)
        self.updateNotebook(objectPages, resetNotebook)
        self.fillWindow(buttonBar, fileView)
    def makeObjectDependentContents(self, object):
        self.fileViewGUI = self.createFileViewGUI(object)
        self.fileViewGUI.addObserver(self.selectionActionGUI)
        buttonBar, objectPages = self.makeActionElements(object)
        fileView = self.fileViewGUI.createView()
        return buttonBar, fileView, objectPages
    def makeActionElements(self, object):
        app = None
        if object.classId() == "test-app":
            app = object
        else:
            app = object.app
        if self.intvActionGUI:
            self.intvActionGUI.disconnectAccelerators()
        self.intvActionGUI = TestActionGUI(self.makeActionInstances(object), self.uiManager, app, object)
        objectPages = self.getObjectNotebookPages(object, self.intvActionGUI)
        return self.intvActionGUI.makeButtons(), objectPages    
    def fillWindow(self, buttonBar, fileView):
        self.window.pack_start(buttonBar, expand=False, fill=False)
        self.topFrame.add(fileView)
        self.window.pack_start(self.vpaned, expand=True, fill=True)
        self.vpaned.show_all()
        self.window.show_all()    
    def createFileViewGUI(self, object):
        if object.classId() == "test-app":
            return ApplicationFileGUI(object, self.dynamic)
        else:
            return TestFileGUI(object, self.dynamic)
    def describePageSwitch(self, notebook, pagePtr, pageNum, *args):
        self.describeNotebook(notebook, pageNum)
    def isVisible(self, notebook):
        if notebook is self.notebook:
            return True
        pageNum = self.notebook.get_current_page()
        page = self.notebook.get_nth_page(pageNum)
        return page is notebook
    def describeAllTabs(self, notebook):
        tabTexts = map(notebook.get_tab_label_text, notebook.get_children())
        guilog.info("")
        guilog.info("Tabs showing : " + string.join(tabTexts, ", "))
    def describeNotebook(self, notebook, pageNum=None):
        if not self.isVisible(notebook):
            return
        outerPageNum, innerPageNum = self.getPageNumbers(notebook, pageNum)
        currentPage, currentPageText = self.getPageText(self.notebook, outerPageNum)
        subPageText = ""
        if isinstance(currentPage, gtk.Notebook):
            subPage, subPageText = self.getPageText(currentPage, innerPageNum)
        pageDesc = self.getPageDescription(currentPageText, subPageText)
        if pageDesc:
            guilog.info("")
            guilog.info(pageDesc)
        # Can get here viewing text info window ...
    def getPageNumbers(self, notebook, pageNum):
        if notebook is self.notebook:
            return pageNum, None
        else:
            return None, pageNum
    def getPageText(self, notebook, pageNum = None):
        if pageNum is None:
            pageNum = notebook.get_current_page()
        page = notebook.get_nth_page(pageNum)
        if page:
            return page, notebook.get_tab_label_text(page)
        else:
            return None, ""
    def getPageDescription(self, currentPageText, subPageText):
        if currentPageText == "Text Info" or subPageText == "Text Info":
            if len(self.testInfo):
                return "---------- Text Info Window ----------\n" + self.testInfo.strip() + "\n" + \
                       "--------------------------------------"
            else:
                return ""
        selectionDesc = self.selectionActionGUI.getPageDescription(currentPageText, subPageText)
        if selectionDesc:
            return selectionDesc
        else:
            return self.intvActionGUI.getPageDescription(currentPageText, subPageText)
    def getWindow(self):
        return self.window
    def makeActionInstances(self, object):
        # File view GUI also has a tab, provide that also...
        return [ self.fileViewGUI.fileViewAction ] + guiplugins.interactiveActionHandler.getInstances(object, self.dynamic)
    def createNotebook(self, objectPages, selectionActionGUI):
        pageDir = selectionActionGUI.createOptionGroupPages()
        pageDir["Test"] = objectPages + pageDir["Test"]
        if len(pageDir) == 1:
            pages = self.addScrollBars(pageDir["Test"])
        else:
            pages = []
            for groupTab, tabPages in pageDir.items():
                if len(tabPages) > 0:
                    scriptTitle = "view sub-options for " + groupTab + " :"
                    tabNotebook = scriptEngine.createNotebook(scriptTitle, self.addScrollBars(tabPages))
                    tabNotebook.show()
                    tabNotebook.connect("switch-page", self.describePageSwitch)
                    pages.append((tabNotebook, groupTab))
                
        notebook = scriptEngine.createNotebook("view options for", pages)
        notebook.connect("switch-page", self.describePageSwitch)
        notebook.show()
        return notebook
    def addScrollBars(self, pages):
        newPages = []
        for widget, name in pages:
            window = self.makeScrolledWindow(widget)
            newPages.append((window, name))
        return newPages
    def makeScrolledWindow(self, widget):
        window = gtk.ScrolledWindow()
        window.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        self.addToScrolledWindow(window, widget)
        window.show()
        return window
    def addToScrolledWindow(self, window, widget):
        if isinstance(widget, gtk.TextView) or isinstance(widget, gtk.Viewport):
            window.add(widget)
        elif isinstance(widget, gtk.VBox):
            # Nasty hack (!?) to avoid progress report (which is so far our only persistent widget) from having the previous window as a parent - remove this and watch TextTest crash when switching between Suite and Test views!
            #if widget.get_parent() != None:
            #    widget.get_parent().remove(widget)
            window.add_with_viewport(widget)
        else:
            raise plugins.TextTestError, "Could not decide how to add scrollbars to " + repr(widget)
    def findTestNotebook(self):
        page = self.findNotebookPage(self.notebook, "Test")
        if page:
            return page
        else:
            return self.notebook
    def findNotebookPage(self, notebook, name):
        for child in notebook.get_children():
            text = notebook.get_tab_label_text(child)
            if text == name:
                return child
    def findChanges(self, newList, oldList):
        created, updated, removed = [], [], []
        for item in newList:
            if item in oldList:
                updated.append(item)
            else:
                created.append(item)
        for item in oldList:
            if not item in newList:
                removed.append(item)
        return created, updated, removed
    def updateNotebook(self, newObjectPages, reset):
        notebook = self.findTestNotebook()
        newPageDir = self.makeDictionary(newObjectPages)
        newObjectPageNames = newPageDir.keys()
        self.diag.info("Updating notebook for " + repr(newObjectPageNames) + " from " + repr(self.oldObjectPageNames))
        currentPage, currentPageName = self.getPageText(notebook)
        pageNamesCreated, pageNamesUpdated, pageNamesRemoved = self.findChanges(newObjectPageNames, self.oldObjectPageNames)
        self.prependNewPages(notebook, pageNamesCreated, newPageDir)
        self.updatePages(notebook, pageNamesUpdated, newPageDir)
        # Must reset if we're viewing a removed page
        reset |= currentPageName in pageNamesRemoved
        # If Text Info is new, it's generally interesting, reset the notebook to view it
        reset |= "Text Info" in pageNamesCreated
        newCurrentPageNum = self.findNewCurrentPageNum(newObjectPageNames, pageNamesRemoved)
        if reset and notebook.get_current_page() != newCurrentPageNum:
            self.diag.info("Resetting for current page " + currentPageName)
            notebook.set_current_page(newCurrentPageNum)
            self.diag.info("Resetting done.")
        elif currentPageName in pageNamesUpdated:
            # describe the current page, we reloaded it...
            self.describeNotebook(notebook)
        self.removePages(notebook, pageNamesRemoved)
        if newObjectPageNames != self.oldObjectPageNames:
            self.oldObjectPageNames = newObjectPageNames
            self.describeAllTabs(notebook)
    def findNewCurrentPageNum(self, newPageNames, pageNamesRemoved):
        for index in range(len(newPageNames)):
            name = newPageNames[index]
            if not name in pageNamesRemoved:
                return index
        return 0
    def prependNewPages(self, notebook, pageNamesCreated, newPageDir):
        # Prepend the pages, hence in reverse order...
        pageNamesCreated.reverse()
        for name in pageNamesCreated:
            self.diag.info("Adding new page " + name)
            newPage = self.makeScrolledWindow(newPageDir[name])
            label = gtk.Label(name)
            notebook.prepend_page(newPage, label)
    def updatePages(self, notebook, pageNamesUpdated, newPageDir):
        for name in pageNamesUpdated:
            self.diag.info("Replacing contents of page " + name)
            # oldPage is a gtk.ScrolledWindow object, newPage either a gtk.VBox or a gtk.TextView
            oldPage = self.findNotebookPage(notebook, name)
            newContents = newPageDir[name]
            self.replaceContents(oldPage, newContents)
    def replaceContents(self, oldPage, newContents):
        oldContents = oldPage.get_child()
        if isinstance(oldContents, gtk.Viewport):
            oldPage = oldContents
            oldContents = oldContents.get_child()
        self.diag.info("Removing old contents " + repr(oldContents))
        oldPage.remove(oldContents)
        oldPage.add(newContents)
        oldPage.show()                
    def removePages(self, notebook, pageNamesRemoved):     
        for name in pageNamesRemoved:
            self.diag.info("Removing page " + name)
            oldPage = self.findNotebookPage(notebook, name)
            notebook.remove(oldPage)
    def getObjectNotebookPages(self, object, intvActionGUI):
        testCasePageDir = intvActionGUI.createOptionGroupPages()["Test"]
        self.testInfo = self.getTestInfo(object)
        if self.testInfo:
            textview = self.createTextView(self.testInfo)
            testCasePageDir = [(textview, "Text Info")] + testCasePageDir
        progressView = self.createProgressView()
        if progressView != None:
            testCasePageDir = [(progressView, "Progress")] + testCasePageDir
        return testCasePageDir
    def getTestInfo(self, test):
        if not test or test.classId() != "test-case":
            return ""
        return test.app.configObject.getTextualInfo(test)
    def createTextView(self, testInfo):
        textview = gtk.TextView()
        textview.set_wrap_mode(gtk.WRAP_WORD)
        textbuffer = textview.get_buffer()

        # Encode to UTF-8, necessary for gtk.TextView
        # First decode using most appropriate encoding ...
        localeEncoding = locale.getdefaultlocale()[1]
        try:
            unicodeInfo = unicode(testInfo, localeEncoding, errors="strict")
        except:
            try:
                guilog.info("Warning: Failed to decode string '" + testInfo + "' using default locale encoding " + repr(localeEncoding) + ". Trying strict UTF-8 encoding ...")
                unicodeInfo = unicode(testInfo, 'utf-8', errors="strict")
            except:
                guilog.info("Warning: Failed to decode string '" + testInfo + "' both using strict UTF-8 and " + repr(localeEncoding) + " encodings.\nReverting to non-strict UTF-8 encoding but replacing problematic\ncharacters with the Unicode replacement character, U+FFFD.")
                unicodeInfo = unicode(testInfo, 'utf-8', errors="replace")
        
        try:
            result = unicodeInfo.encode('utf-8', 'strict')
        except:
            try:
                guilog.info("Warning: Failed to encode Unicode string '" + unicodeInfo + "' using strict UTF-8 encoding.\nReverting to non-strict UTF-8 encoding but replacing problematic\ncharacters with the Unicode replacement character, U+FFFD.")
                result = unicodeInfo.encode('utf-8', 'replace')
            except:
                guilog.info("Warning: Failed to encode Unicode string '" + unicodeInfo + "' using both strict UTF-8 encoding and UTF-8 encoding with replacement. Showing error message instead.")
                result = "Failed to encode Unicode string."

        textbuffer.set_text(result)        
        textview.show()
        return textview
    def createProgressView(self):
        if self.progressMonitor != None:
            return self.progressMonitor.getProgressView()
        else:
            return None
        
class FileViewGUI(plugins.Observable):
    def __init__(self, object, dynamic):
        plugins.Observable.__init__(self)
        self.fileViewAction = guiplugins.interactiveActionHandler.getFileViewer(object, dynamic)
        self.model = gtk.TreeStore(gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_STRING,\
                                   gobject.TYPE_PYOBJECT, gobject.TYPE_STRING)
        self.name = object.name.replace("_", "__")
        self.selection = None
        self.dynamic = dynamic
    def addFileToModel(self, iter, name, comp, colour):
        fciter = self.model.insert_before(iter, None)
        baseName = os.path.basename(name)
        heading = self.model.get_value(iter, 0)
        self.model.set_value(fciter, 0, baseName)
        self.model.set_value(fciter, 1, colour)
        self.model.set_value(fciter, 2, name)
        guilog.info("Adding file " + baseName + " under heading '" + heading + "', coloured " + colour)
        if comp:
            self.model.set_value(fciter, 3, comp)
            details = comp.getDetails()
            if len(details) > 0:
                self.model.set_value(fciter, 4, details)
                guilog.info("(Second column '" + details + "' coloured " + colour + ")")
        return fciter
    def createView(self):
        # blank line for demarcation
        guilog.info("")
        # defined in subclasses
        self.addFilesToModel()
        fileWin = gtk.ScrolledWindow()
        fileWin.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
        view = gtk.TreeView(self.model)
        self.selection = view.get_selection()
        self.selection.set_mode(gtk.SELECTION_MULTIPLE)
        renderer = gtk.CellRendererText()
        column = gtk.TreeViewColumn(self.name, renderer, text=0, background=1)
        column.set_cell_data_func(renderer, renderParentsBold)
        view.append_column(column)
        if self.dynamic:
            perfColumn = gtk.TreeViewColumn("Details", renderer, text=4)
            view.append_column(perfColumn)
        view.expand_all()
        indexer = TreeModelIndexer(self.model, column, 0)
        scriptEngine.connect("select file", "row_activated", view, self.displayFile, indexer)
        scriptEngine.monitor("set file selection to", self.selection, indexer)
        self.selectionChanged(self.selection)
        view.get_selection().connect("changed", self.selectionChanged)
        view.show()
        fileWin.add(view)
        fileWin.show()
        return fileWin
    def selectionChanged(self, selection):
        filelist = []
        selection.selected_foreach(self.fileSelected, filelist)
        self.notify("NewFileSelection", filelist)
    def fileSelected(self, treemodel, path, iter, filelist):
        filelist.append(self.model.get_value(iter, 0))
    def displayFile(self, view, path, column, *args):
        iter = self.model.get_iter(path)
        fileName = self.model.get_value(iter, 2)
        if not fileName:
            # Don't crash on double clicking the header lines...
            return
        comparison = self.model.get_value(iter, 3)
        try:
            self.fileViewAction.view(comparison, fileName)
            self.selection.unselect_all()
        except plugins.TextTestError, e:
            showError(str(e))

class ApplicationFileGUI(FileViewGUI):
    def __init__(self, app, dynamic):
        FileViewGUI.__init__(self, app, dynamic)
        self.app = app
    def addFilesToModel(self):
        confiter = self.model.insert_before(None, None)
        self.model.set_value(confiter, 0, "Application Files")
        colour = self.app.getConfigValue("file_colours")["app_static"]
        for file in self.getConfigFiles():
            self.addFileToModel(confiter, file, None, colour)

        personalFiles = self.getPersonalFiles()
        if len(personalFiles) > 0:
            persiter = self.model.insert_before(None, None)
            self.model.set_value(persiter, 0, "Personal Files")
            for file in personalFiles:
                self.addFileToModel(persiter, file, None, colour)
    def getConfigFiles(self):
        configFiles = self.app.dircache.findAllFiles("config", [ self.app.name ])
        configFiles.sort()
        return configFiles
    def getPersonalFiles(self):
        personalFiles = []
        personalFile = self.app.getPersonalConfigFile()
        if personalFile:
            personalFiles.append(personalFile)
        gtkRcFile = getGtkRcFile()
        if gtkRcFile:
            personalFiles.append(gtkRcFile)
        return personalFiles
    
class TestFileGUI(FileViewGUI):
    def __init__(self, test, dynamic):
        FileViewGUI.__init__(self, test, dynamic)
        self.test = test
        self.colours = test.getConfigValue("file_colours")
        test.refreshFiles()
        self.testComparison = None
    def addFilesToModel(self):
        if self.test.state.hasStarted():
            try:
                self.addDynamicFilesToModel(self.test)
            except AttributeError:
                # The above code assumes we have failed on comparison: if not, don't display things
                pass
        else:
            self.addStaticFilesToModel(self.test)
    def addDynamicFilesToModel(self, test):
        self.testComparison = test.state
        if not test.state.isComplete():
            self.testComparison = comparetest.TestComparison(test.state, test.app)
            self.testComparison.makeComparisons(test, testInProgress=1)

        self.addDynamicComparisons(self.testComparison.correctResults + self.testComparison.changedResults, "Comparison Files")
        self.addDynamicComparisons(self.testComparison.newResults, "New Files")
        self.addDynamicComparisons(self.testComparison.missingResults, "Missing Files")
    def addDynamicComparisons(self, compList, title):
        if len(compList) == 0:
            return
        iter = self.model.insert_before(None, None)
        self.model.set_value(iter, 0, title)
        filelist = []
        fileCompMap = {}
        for comp in compList:
            file = comp.getDisplayFileName()
            fileCompMap[file] = comp
            filelist.append(file)
        filelist.sort()
        self.addStandardFilesUnderIter(iter, filelist, fileCompMap)
    def addStandardFilesUnderIter(self, iter, files, compMap = {}):
        for relDir, relDirFiles in self.classifyByRelDir(files).items():
            iterToUse = iter
            if relDir:
                iterToUse = self.addFileToModel(iter, relDir, None, self.getStaticColour())
            for file in relDirFiles:
                comparison = compMap.get(file)
                colour = self.getComparisonColour(comparison)
                self.addFileToModel(iterToUse, file, comparison, colour)
    def classifyByRelDir(self, files):
        dict = {}
        for file in files:
            relDir = self.getRelDir(file)
            if not dict.has_key(relDir):
                dict[relDir] = []
            dict[relDir].append(file)
        return dict
    def getRelDir(self, file):
        relPath = self.test.getTestRelPath(file)
        if relPath.find(os.sep) != -1:
            dir, local = os.path.split(relPath)
            return dir
        else:
            return ""
    def getComparisonColour(self, fileComp):
        if not fileComp:
            return self.getStaticColour()
        if not self.test.state.isComplete():
            return self.colours["running"]
        if fileComp.hasSucceeded():
            return self.colours["success"]
        else:
            return self.colours["failure"]
    def getStaticColour(self):
        if self.dynamic:
            return self.colours["not_started"]
        else:
            return self.colours["static"]
    def addStaticFilesToModel(self, test):
        stdFiles, defFiles = test.listStandardFiles(allVersions=True)
        if test.classId() == "test-case":
            stditer = self.model.insert_before(None, None)
            self.model.set_value(stditer, 0, "Standard Files")
            if len(stdFiles):
                self.addStandardFilesUnderIter(stditer, stdFiles)

        defiter = self.model.insert_before(None, None)
        self.model.set_value(defiter, 0, "Definition Files")
        self.addStandardFilesUnderIter(defiter, defFiles)
        self.addStaticDataFilesToModel(test)
    def getDisplayDataFiles(self, test):
        try:
            return test.app.configObject.extraReadFiles(test).items()
        except:
            sys.stderr.write("WARNING - ignoring exception thrown by '" + test.app.configObject.moduleName + \
                             "' configuration while requesting extra data files, not displaying any such files")
            plugins.printException()
            return seqdict()
    def addStaticDataFilesToModel(self, test):
        dataFiles = test.listDataFiles()
        displayDataFiles = self.getDisplayDataFiles(test)
        if len(dataFiles) == 0 and len(displayDataFiles) == 0:
            return
        datiter = self.model.insert_before(None, None)
        self.model.set_value(datiter, 0, "Data Files")
        colour = self.getStaticColour()
        self.addDataFilesUnderIter(test, datiter, dataFiles, colour)
        for name, filelist in displayDataFiles:
            exiter = self.model.insert_before(datiter, None)
            self.model.set_value(exiter, 0, name)
            for file in filelist:
                self.addFileToModel(exiter, file, None, colour)
    def addDataFilesUnderIter(self, test, iter, files, colour):
        dirIters = { test.getDirectory() : iter }
        parentIter = iter
        for file in files:
            parent, local = os.path.split(file)
            parentIter = dirIters[parent]
            newiter = self.addFileToModel(parentIter, file, None, colour)
            if os.path.isdir(file):
                dirIters[file] = newiter
    
# Class for importing self tests
class ImportTestCase(guiplugins.ImportTestCase):
    def addDefinitionFileOption(self, suite, oldOptionGroup):
        guiplugins.ImportTestCase.addDefinitionFileOption(self, suite, oldOptionGroup)
        self.addSwitch(oldOptionGroup, "GUI", "Use TextTest GUI", 1)
        self.addSwitch(oldOptionGroup, "sGUI", "Use TextTest Static GUI", 0)
        targetApp = self.test.makePathName("TargetApp")
        root, local = os.path.split(targetApp)
        self.defaultTargetApp = plugins.samefile(root, self.test.app.getDirectory())
        if self.defaultTargetApp:
            self.addSwitch(oldOptionGroup, "sing", "Only run test A03", 1)
            self.addSwitch(oldOptionGroup, "fail", "Include test failures", 1)
            self.addSwitch(oldOptionGroup, "version", "Run with Version 2.4")
    def getOptions(self, suite):
        options = guiplugins.ImportTestCase.getOptions(self, suite)
        if self.optionGroup.getSwitchValue("sGUI"):
            options += " -gx"
        elif self.optionGroup.getSwitchValue("GUI"):
            options += " -g"
        if self.defaultTargetApp:
            if self.optionGroup.getSwitchValue("sing"):
                options += " -t A03"
            if self.optionGroup.getSwitchValue("fail"):
                options += " -c CodeFailures"
            if self.optionGroup.getSwitchValue("version"):
                options += " -v 2.4"
        return options

# A utility class to set and get the indices of options in radio button groups.
class RadioGroupIndexer:
    def __init__(self, listOfButtons):
        self.buttons = listOfButtons
    def getActiveIndex(self):
        for i in xrange(0, len(self.buttons)):
            if self.buttons[i].get_active():
                return i
    def setActiveIndex(self, index):
        self.buttons[index].set_active(True)
        
#
# A simple wrapper class around a gtk.StatusBar, simplifying
# logging messages/changing the status bar implementation.
# 
class GUIStatusMonitor:
    def __init__(self):
        self.statusBar = gtk.Statusbar()
        self.diag = plugins.getDiagnostics("GUI status monitor")
        self.notifyStatus("TextTest started at " + plugins.localtime() + ".")

    def notifyStatus(self, message):
        self.diag.info("Changing GUI status to: '" + message + "'")
        self.statusBar.push(0, message)
        
    def createStatusbar(self):
        self.statusBar.show()
        self.statusBarEventBox = gtk.EventBox()
        self.statusBarEventBox.add(self.statusBar)
        self.statusBarEventBox.show()
        return self.statusBarEventBox

class TestProgressBar:
    def __init__(self, totalNofTests):
        self.totalNofTests = totalNofTests
        self.nofCompletedTests = 0
        self.nofFailedTests = 0
        self.progressBar = None
    def createProgressBar(self):
        self.progressBar = gtk.ProgressBar()
        self.resetBar()
        self.progressBar.show()
        return self.progressBar
    def adjustToSpace(self, windowWidth):
        self.progressBar.set_size_request(int(windowWidth * 0.75), 1)
    def notifyLifecycleChange(self, test, state, changeDesc):
        failed = state.hasFailed()
        if changeDesc == "complete":
            self.nofCompletedTests += 1
            if failed:
                self.nofFailedTests += 1
            self.resetBar()
        elif state.isComplete() and not failed: # test saved, possibly partially so still check 'failed'
            self.nofFailedTests -= 1
            self.adjustFailCount()
    def resetBar(self):
        message = self.getFractionMessage()
        message += self.getFailureMessage(self.nofFailedTests)
        fraction = float(self.nofCompletedTests) / float(self.totalNofTests)
        guilog.info("Progress bar set to fraction " + str(fraction) + ", text '" + message + "'")
        self.progressBar.set_text(message)
        self.progressBar.set_fraction(fraction)
    def getFractionMessage(self):
        if self.nofCompletedTests >= self.totalNofTests:
            completionTime = plugins.localtime()
            return "All " + str(self.totalNofTests) + " tests completed at " + completionTime
        else:
            return str(self.nofCompletedTests) + " of " + str(self.totalNofTests) + " tests completed"
    def getFailureMessage(self, failCount):
        if failCount != 0:
            return " (" + str(failCount) + " tests failed)"
        else:
            return ""
    def adjustFailCount(self):
        message = self.progressBar.get_text()
        oldFailMessage = self.getFailureMessage(self.nofFailedTests + 1)
        newFailMessage = self.getFailureMessage(self.nofFailedTests)
        message = message.replace(oldFailMessage, newFailMessage)
        guilog.info("Progress bar detected save, new text is '" + message + "'")
        self.progressBar.set_text(message)
            
# Class that keeps track of (and possibly shows) the progress of
# pending/running/completed tests
class TestProgressMonitor(plugins.Observable):
    def __init__(self):
        plugins.Observable.__init__(self)
        self.classifications = {} # map from test to list of iterators where it exists
                
        # Each row has 'type', 'number', 'show', 'tests'
        self.treeModel = gtk.TreeStore(gobject.TYPE_STRING, gobject.TYPE_INT, gobject.TYPE_BOOLEAN, \
                                       gobject.TYPE_STRING, gobject.TYPE_STRING, gobject.TYPE_PYOBJECT)
        self.progressReport = None
        self.treeView = None
        self.setupTreeView()
    def setupTreeView(self):
        self.treeView = gtk.TreeView(self.treeModel)
        selection = self.treeView.get_selection()
        selection.set_mode(gtk.SELECTION_MULTIPLE)
        selection.connect("changed", self.selectionChanged)
        textRenderer = gtk.CellRendererText()
        numberRenderer = gtk.CellRendererText()
        numberRenderer.set_property('xalign', 1)
        statusColumn = gtk.TreeViewColumn("Status", textRenderer, text=0, background=3, font=4)
        numberColumn = gtk.TreeViewColumn("Number", numberRenderer, text=1, background=3, font=4)
        self.treeView.append_column(statusColumn)
        self.treeView.append_column(numberColumn)
        toggle = gtk.CellRendererToggle()
        toggle.set_property('activatable', True)
        indexer = TreeModelIndexer(self.treeModel, statusColumn, 0)
        scriptEngine.connect("toggle progress report category ", "toggled", toggle, self.showToggled, indexer)
        scriptEngine.monitor("set progress report filter selection to", selection, indexer)
        toggleColumn = gtk.TreeViewColumn("Visible", toggle, active=2)
        toggleColumn.set_alignment(0.5)
        self.treeView.append_column(toggleColumn)
        
        self.progressReport = gtk.VBox()
        self.progressReport.pack_start(self.treeView, expand=True, fill=True)
        self.progressReport.show_all()
            
    def selectionChanged(self, selection):
        # For each selected row, select the corresponding rows in the test treeview
        tests = []
        selection.selected_foreach(self.selectCorrespondingTests, tests)
        self.notify("NewTestSelection", tests)
    def selectCorrespondingTests(self, treemodel, path, iter, tests , *args):
        guilog.info("Selecting all " + str(treemodel.get_value(iter, 1)) + " tests in category " + treemodel.get_value(iter, 0))
        tests += treemodel.get_value(iter, 5)
    def findTestIterators(self, test):
        return self.classifications.get(test, [])
    def getCategoryDescription(self, state):
        briefDesc, fullDesc = state.categoryDescriptions.get(state.category, (state.category, state.category))
        return briefDesc.replace("_", " ").capitalize()
    def getClassifiers(self, state):
        catDesc = self.getCategoryDescription(state)
        if not state.isComplete() or not state.hasFailed():
            return [ catDesc ]
        classifiers = [ "Failed" ]
        if self.isPerformance(catDesc):
            classifiers += [ "Performance differences", catDesc ]
        else:
            briefText = state.getBriefClassifier()
            if catDesc == "Failed":
                classifiers += [ "Differences", briefText ]
            else:
                classifiers += [ catDesc, briefText ]
        return classifiers
    def isPerformance(self, catDesc):
        for perfCat in [ "Slower", "Faster", "Memory" ]:
            if catDesc.find(perfCat) != -1:
                return True
        return False
    def removeTest(self, test):
        for iter in self.findTestIterators(test):
            testCount = self.treeModel.get_value(iter, 1)
            self.treeModel.set_value(iter, 1, testCount - 1)
            if testCount == 1:
                self.treeModel.set_value(iter, 3, "white")
                self.treeModel.set_value(iter, 4, "")
            allTests = self.treeModel.get_value(iter, 5)
            allTests.remove(test)
            self.treeModel.set_value(iter, 5, allTests)
    def insertTest(self, test, state):
        searchIter = self.treeModel.get_iter_root()
        parentIter = None
        self.classifications[test] = []
        classifiers = self.getClassifiers(state)
        for classifier in classifiers:
            iter = self.findIter(classifier, searchIter)
            if iter:
                self.insertTestAtIter(iter, test, state.category)
                searchIter = self.treeModel.iter_children(iter)
            else:
                iter = self.addNewIter(classifier, parentIter, test, state.category)
                searchIter = None
            parentIter = iter
            self.classifications[test].append(iter)
        return iter
    def insertTestAtIter(self, iter, test, category):
        allTests = self.treeModel.get_value(iter, 5)
        testCount = self.treeModel.get_value(iter, 1)
        if testCount == 0:
            self.treeModel.set_value(iter, 3, getTestColour(test, category))
            self.treeModel.set_value(iter, 4, "bold")
        self.treeModel.set_value(iter, 1, testCount + 1)
        allTests.append(test)
        self.treeModel.set_value(iter, 5, allTests)
    def addNewIter(self, classifier, parentIter, test, category):
        showThis = self.showByDefault(test, category)
        modelAttributes = [classifier, 1, showThis, getTestColour(test, category), "bold", [ test ]]
        newIter = self.treeModel.append(parentIter, modelAttributes)
        if parentIter:
            self.treeView.expand_row(self.treeModel.get_path(parentIter), open_all=0)
        return newIter
    def findIter(self, classifier, startIter):
        iter = startIter
        while iter != None:
            name = self.treeModel.get_value(iter, 0)
            if name == classifier:
                return iter
            else:
                iter = self.treeModel.iter_next(iter)
    # Set default values for toggle buttons in the TreeView, based
    # on the config files.
    def showByDefault(self, test, category):
        # Check config files
        return category.lower() not in test.getConfigValue("hide_test_category")
    def notifyLifecycleChange(self, test, state, changeDesc):
        self.removeTest(test)
        newIter = self.insertTest(test, state)
        self.notify("Visibility", test, self.treeModel.get_value(newIter, 2)) 
        self.diagnoseTree()   
    def diagnoseTree(self):
        guilog.info("Test progress:")
        childIters = []
        childIter = self.treeModel.get_iter_root()

        # Put all children in list to be treated
        while childIter != None:
            childIters.append(childIter)
            childIter = self.treeModel.iter_next(childIter)

        while len(childIters) > 0:
            childIter = childIters[0]
            # If this iter has children, add these to the list to be treated
            if self.treeModel.iter_has_child(childIter):                            
                subChildIter = self.treeModel.iter_children(childIter)
                pos = 1
                while subChildIter != None:
                    childIters.insert(pos, subChildIter)
                    pos = pos + 1
                    subChildIter = self.treeModel.iter_next(subChildIter)
            # Print the iter
            indentation = ("--" * (self.getIterDepth(childIter) + 1)) + "> "
            name = self.treeModel.get_value(childIter, 0)
            count = str(self.treeModel.get_value(childIter, 1))
            bg = self.treeModel.get_value(childIter, 3)
            font = self.treeModel.get_value(childIter, 4)
            guilog.info(indentation + name + " : " + count + ", colour '" + bg + "', font '" + font + "'")
            childIters = childIters[1:len(childIters)]

    def getIterDepth(self, iter):
        parent = self.treeModel.iter_parent(iter)
        depth = 0
        while parent != None:
            depth = depth + 1
            parent = self.treeModel.iter_parent(parent)
        return depth
   
    def getAllChildIters(self, iter):
         # Toggle all children too
        childIters = []
        childIter = self.treeModel.iter_children(iter)
        while childIter != None:
            childIters.append(childIter)
            childIters += self.getAllChildIters(childIter)
            childIter = self.treeModel.iter_next(childIter)
        return childIters
    def showToggled(self, cellrenderer, path):
        # Toggle the toggle button
        newValue = not self.treeModel[path][2]
        self.treeModel[path][2] = newValue

        # Print some gui log info
        iter = self.treeModel.get_iter_from_string(path)
        if self.treeModel.get_value(iter, 2) == 1:
            guilog.info("Selecting to show tests in the '" + self.treeModel.get_value(iter, 0) + "' category.")
        else:
            guilog.info("Selecting not to show tests in the '" + self.treeModel.get_value(iter, 0) + "' category.")

        for childIter in self.getAllChildIters(iter):
            self.treeModel.set_value(childIter, 2, newValue)

        # Now, re-filter the main treeview to be consistent with
        # the chosen progress report options.
        for test in self.treeModel.get_value(iter, 5):
            self.notify("Visibility", test, newValue)
                    
    def getProgressView(self):
        return self.progressReport
