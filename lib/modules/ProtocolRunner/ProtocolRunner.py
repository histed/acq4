# -*- coding: utf-8 -*-
#from __future__ import with_statement
from lib.modules.Module import *
from ProtocolRunnerTemplate import *
from PyQt4 import QtGui, QtCore
#from DirTreeModel import *
import DirTreeWidget
import configfile
from collections import OrderedDict
from SequenceRunner import *
#from pyqtgraph.WidgetGroup import WidgetGroup
from Mutex import Mutex, MutexLocker
from lib.Manager import getManager, logMsg, logExc
from debug import *
import ptime
import analysisModules
import time, gc
#import sip
import sys, os
from HelpfulException import HelpfulException
#from lib.LogWindow import LogButton
import pyqtgraph as pg
from StatusBar import StatusBar


#import pdb

class Window(QtGui.QMainWindow):
    def __init__(self, pr):
        QtGui.QMainWindow.__init__(self)
        mp = os.path.dirname(__file__)
        self.setWindowIcon(QtGui.QIcon(os.path.join(mp, 'icon.png')))
        self.pr = pr

        self.stateFile = os.path.join('modules', self.pr.name + '_ui.cfg')
        uiState = getManager().readConfigFile(self.stateFile)
        if 'geometry' in uiState:
            geom = QtCore.QRect(*uiState['geometry'])
            self.setGeometry(geom)
            #print "set geometry", geom

        
    def closeEvent(self, ev):
        geom = self.geometry()
        uiState = {'geometry': [geom.x(), geom.y(), geom.width(), geom.height()]}
        getManager().writeConfigFile(uiState, self.stateFile)
        
        self.pr.quit()
        #ev.ignore()
        #sip.delete(self)

class Loader(DirTreeWidget.DirTreeLoader):
    def __init__(self, host, baseDir):
        DirTreeWidget.DirTreeLoader.__init__(self, baseDir, create=True)
        self.host = host

    def new(self):
        self.host.newProtocol()
        return True
    
    def load(self, handle):
        self.host.loadProtocol(handle)
        return True
        
    def save(self, handle):
        self.host.saveProtocol(handle)
        return True
        
        
        
class ProtocolRunner(Module):
    
    sigProtocolPaused = QtCore.Signal()
    sigProtocolFinished = QtCore.Signal()
    sigNewFrame = QtCore.Signal(object)
    sigProtocolStarted = QtCore.Signal(object)  ## called whenever single protocol OR protocol sequence has started
    sigTaskStarted = QtCore.Signal(object)      ## called at start of EVERY protocol, including within sequences
    sigProtocolChanged = QtCore.Signal(object, object)
    
    def __init__(self, manager, name, config):
        Module.__init__(self, manager, name, config)
        self.lastProtoTime = None
        self.loopEnabled = False
        self.devListItems = {}
        
        self.docks = {}
        self.analysisDocks = {}
        self.deleteState = 0
        self.ui = Ui_MainWindow()
        self.win = Window(self)
        
        g = self.win.geometry()
        self.ui.setupUi(self.win)
        self.win.setGeometry(g)
        
        #self.logBtn = LogButton("Log")
        #self.win.statusBar().addPermanentWidget(self.logBtn)
        self.win.setStatusBar(StatusBar())
        
        self.ui.protoDurationSpin.setOpts(dec=True, bounds=[1e-3,None], step=1, minStep=1e-3, suffix='s', siPrefix=True)
        self.ui.protoLeadTimeSpin.setOpts(dec=True, bounds=[0,None], step=1, minStep=10e-3, suffix='s', siPrefix=True)
        self.ui.protoCycleTimeSpin.setOpts(dec=True, bounds=[0,None], step=1, minStep=1e-3, suffix='s', siPrefix=True)
        self.ui.seqCycleTimeSpin.setOpts(dec=True, bounds=[0,None], step=1, minStep=1e-3, suffix='s', siPrefix=True)
        self.protoStateGroup = pg.WidgetGroup([
            (self.ui.protoContinuousCheck, 'continuous'),
            (self.ui.protoDurationSpin, 'duration'),
            (self.ui.protoLeadTimeSpin, 'leadTime'),
            (self.ui.protoLoopCheck, 'loop'),
            (self.ui.protoCycleTimeSpin, 'loopCycleTime'),
            (self.ui.seqCycleTimeSpin, 'cycleTime'),
            (self.ui.seqRepetitionSpin, 'repetitions', 1),
        ])
        
        try:
            self.protocolList = Loader(self, self.manager.config['protocolDir'])
        except KeyError:
            raise HelpfulException("Config is missing 'protocolDir'; cannot load protocol list.")
        
        self.ui.LoaderDock.setWidget(self.protocolList)
        
        self.currentProtocol = None   ## pointer to current protocol object
        
        for m in analysisModules.MODULES:
            item = QtGui.QListWidgetItem(m, self.ui.analysisList)
            item.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable )
            item.setCheckState(QtCore.Qt.Unchecked)
        
        self.taskThread = TaskThread(self)
        
        self.newProtocol()
        
        self.ui.testSingleBtn.clicked.connect(self.testSingleClicked)
        self.ui.runProtocolBtn.clicked.connect(self.runSingleClicked)
        self.ui.testSequenceBtn.clicked.connect(self.testSequence)
        self.ui.runSequenceBtn.clicked.connect(self.runSequenceClicked)
        self.ui.stopSingleBtn.clicked.connect(self.stopSingle)
        self.ui.stopSequenceBtn.clicked.connect(self.stopSequence)
        self.ui.pauseSequenceBtn.toggled.connect(self.pauseSequence)
        self.ui.deviceList.itemClicked.connect(self.deviceItemClicked)
        self.protocolList.sigCurrentFileChanged.connect(self.fileChanged)  ## called if loaded protocol file is renamed or moved
        self.taskThread.finished.connect(self.taskThreadStopped)
        self.taskThread.sigNewFrame.connect(self.handleFrame)
        self.taskThread.sigPaused.connect(self.taskThreadPaused)
        self.taskThread.sigTaskStarted.connect(self.taskStarted)
        self.taskThread.sigExitFromError.connect(self.taskErrored)
        self.protoStateGroup.sigChanged.connect(self.protoGroupChanged)
        self.win.show()
        self.ui.sequenceParamList.itemChanged.connect(self.updateSeqReport)
        self.ui.analysisList.itemClicked.connect(self.analysisItemClicked)
        
        
    def protoGroupChanged(self, param, value):
        #self.emit(QtCore.SIGNAL('protocolChanged'), param, value)
        self.sigProtocolChanged.emit(param, value)
        if param == 'repetitions':
            self.updateSeqParams()
        if param in ['duration', 'cycleTime', 'leadTime']:
            self.updateSeqReport()
        
    def getDevice(self, dev):
        """Return the protocolGui for dev. Used by some devices to detect changes in others."""
        if dev not in self.docks:
            ## Create the device if needed
            try:
                item = self.ui.deviceList.findItems(dev, QtCore.Qt.MatchExactly)[0]
            except:
                raise Exception('Requested device %s does not exist!' % dev)
            item.setCheckState(QtCore.Qt.Checked)
            self.deviceItemClicked(item)
            #self.docks[dev].show()
        return self.docks[dev].widget()
        
    def getParam(self, param):
        """Return the value of a named protocol parameter"""
        return self.protoStateGroup.state()[param]
        
    def updateDeviceList(self, protocol=None):
        """Update the device list to reflect only the devices that exist in the system or are referenced by the current protocol. Update the color and checkstate of each item as well."""
        devList = self.manager.listDevices()
        
        if protocol is not None:
            protList = protocol.devices.keys()
        elif self.currentProtocol is not None:
            protList = self.currentProtocol.devices.keys()
        else:
            protList = []
            
        ## Remove all devices that do not exist and are not referenced by the protocol
        rem = []
        for d in self.devListItems:
            if d not in devList and d not in protList:
                #print "    ", d
                self.ui.deviceList.takeItem(self.ui.deviceList.row(self.devListItems[d]))
                rem.append(d)
        for d in rem:
            del self.devListItems[d]
                
        ## Add all devices that exist in the current system
        for d in devList:
            if d not in self.devListItems:
                self.devListItems[d] = QtGui.QListWidgetItem(d, self.ui.deviceList)
                #self.devListItems[d].setData(32, QtCore.QVariant(d))
                self.devListItems[d].setData(32, d)
            self.devListItems[d].setForeground(QtGui.QBrush(QtGui.QColor(0,0,0)))
            
            
        ## Add all devices that are referenced by the protocol but do not exist
        
        for d in protList:
            if d not in self.devListItems:
                self.devListItems[d] = QtGui.QListWidgetItem(d, self.ui.deviceList)
                self.devListItems[d].setForeground(QtGui.QBrush(QtGui.QColor(150,0,0)))
            
        ## Make sure flags and checkState are correct for all items
        for d in self.devListItems:
            self.devListItems[d].setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsUserCheckable )
            if d in protList:
                self.devListItems[d].setCheckState(QtCore.Qt.Checked)
            else:
                self.devListItems[d].setCheckState(QtCore.Qt.Unchecked)
        
    def deviceItemClicked(self, item):
        """Respond to clicks in the device list. Add/remove devices from the current protocol and update docks."""
        name = str(item.text())
        if item.checkState() == QtCore.Qt.Unchecked:
            self.currentProtocol.removeDevice(name)
        else:
            self.currentProtocol.addDevice(name)
        self.updateDeviceDocks([name])
            
    #def deviceItemChanged(self, item):
        #newName = str(item.text())
        #oldName = str(item.data(32).toString())
        #if newName == oldName:
            #return
        
        ### If the new name does exist:
          ### If the types are compatible, rename and update the new dock
          ### If the types are incompatible, reject the rename
          
          
        #if newName in self.devListItems:
            ### Destroy old dock if needed
            #if newName in self.currentProtocol.enabledDevices():
                #self.devListItems[newName].setCheckState(QtCore.Qt.Unchecked)
                #self.updateDeviceDocks()
            ### remove from list
            #self.ui.deviceList.takeItem(self.devListItems[newName])
          
        ### if the new name doesn't exist, just accept the rename and update the device list
            
        #item.setData(32, QtCore.QVariant(newName))
        #self.devListItems[newName] = item
        #del self.devListItems[oldName]
        #self.currentProtocol.renameDevice(oldName, newName)
        #self.updateDeviceList()
        
        ### If the new name is an existing device, load and configure its dock
        #if newName in self.manager.listDevices():
            #self.updateDeviceDocks()
        
        ### Configure docks
        #if newName in self.docks:
            #self.docks[newName].widget().restoreState(self.currentProtocol.conf['devices'][newName])
            
            ### Configure dock positions
            #if 'winState' in self.currentProtocol.conf:
                #self.win.restoreState(QtCore.QByteArray.fromPercentEncoding(self.currentProtocol.conf['winState']))
            
        
    def analysisItemClicked(self, item):
        name = str(item.text())
        if item.checkState() == QtCore.Qt.Checked:
            if not self.createAnalysisDock(name):
                item.setCheckState(QtCore.Qt.Unchecked)
        else:
            self.removeAnalysisDock(name)
        
    def createAnalysisDock(self, mod):
        try:
            m = analysisModules.createAnalysisModule(mod, self)
            dock = QtGui.QDockWidget(mod)
            dock.setFeatures(dock.AllDockWidgetFeatures)
            dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea|QtCore.Qt.TopDockWidgetArea)
            dock.setObjectName(mod)
            dock.setWidget(m)
            dock.setAutoFillBackground(True)
            
            self.analysisDocks[mod] = dock
            self.win.addDockWidget(QtCore.Qt.BottomDockWidgetArea, dock)
            
            items = self.ui.analysisList.findItems(mod, QtCore.Qt.MatchExactly)
            items[0].setCheckState(QtCore.Qt.Checked)
            
            return True
        except:
            printExc("Analysis module creation failed:")
            return False
        
    def removeAnalysisDock(self, mod):
        if mod not in self.analysisDocks:
            return
        try:
            self.analysisDocks[mod].widget().quit()
        except:
            printExc("Error closing analysis dock:")
        self.win.removeDockWidget(self.analysisDocks[mod])
        #sip.delete(self.analysisDocks[mod])
        del self.analysisDocks[mod]
        items = self.ui.analysisList.findItems(mod, QtCore.Qt.MatchExactly)
        items[0].setCheckState(QtCore.Qt.Unchecked)
        
        
    #def protoListClicked(self, ind):
        #sel = list(self.ui.protocolList.selectedIndexes())
        #if len(sel) == 1:
            #self.ui.deleteProtocolBtn.setEnabled(True)
        #else:
            #self.ui.deleteProtocolBtn.setEnabled(False)
        #self.resetDeleteState()
            
    def fileChanged(self, handle, change, args):
        if change == 'renamed' or change == 'moved':
            self.currentProtocol.fileName = handle.name()
            
        
        
    #def fileRenamed(self, fn1, fn2):
        #"""Update the current protocol state to follow a file that has been moved or renamed"""
        #if fn1 == self.currentProtocol.fileName:
            #self.currentProtocol.fileName = fn2
            #pn = fn2.replace(self.protocolList.baseDir, '')
            #self.ui.currentProtocolLabel.setText(pn)
            #return
        #if os.path.isdir(fn2) and fn1 in self.currentProtocol.fileName:
            #self.currentProtocol.fileName = self.currentProtocol.fileName.replace(fn1, fn2)
            #pn = self.currentProtocol.fileName.replace(self.protocolList.baseDir, '')
            #self.ui.currentProtocolLabel.setText(pn)
            #return
            
    def updateSeqParams(self, dev='protocol'):
        """Update the list of available sequence parameters."""
        if dev == 'protocol':
            rep = self.protoStateGroup.state()['repetitions']
            if rep == 0:
                params = {}
            else:
                params = {'repetitions': range(rep)}
        elif dev not in self.currentProtocol.enabledDevices():
            return
        else:
            params = self.docks[dev].widget().listSequence()
        #print "New parameter lst:", params
        self.ui.sequenceParamList.updateList(dev, params)
        
        self.updateSeqReport()
        
    def updateSeqReport(self):
        s = self.protoStateGroup.state()
        period = max(s['duration']+s['leadTime'], s['cycleTime'])
        items = self.ui.sequenceParamList.listParams()[:]
        if len(items) == 0:
            self.ui.paramSpaceLabel.setText('0')
            self.ui.seqTimeLabel.setText('0')
            tot = 0
        else:
            #ps = [str(i.text(2)) for i in items]
            psi = [len(i[2]) for i in items]
            ps = map(str, psi)
            tot = reduce(lambda x,y: x*y, psi)
            self.ui.paramSpaceLabel.setText(' x '.join(ps) + ' = %d' % tot)
            self.ui.seqTimeLabel.setText('%0.3f sec' % (period*tot))
            
        if tot == 0:
            self.ui.testSequenceBtn.setEnabled(False)
            self.ui.runSequenceBtn.setEnabled(False)
        else:
            self.ui.testSequenceBtn.setEnabled(True)
            self.ui.runSequenceBtn.setEnabled(True)
            
        
    def hideDock(self, dev):
        self.docks[dev].hide()
        self.docks[dev].widget().disable()
        self.ui.sequenceParamList.removeDevice(dev)
        
    def showDock(self, dev):
        self.docks[dev].show()
        self.docks[dev].widget().enable()
        self.updateSeqParams(dev)
        #items = self.ui.sequenceParamList.findItems(dev, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
        #for i in items:
            #i.setHidden(False)
        
    def updateDeviceDocks(self, devNames=None):
        """Create/unhide new docks if they are needed and hide old docks if they are not.
        If a list of device names is given, only those device docks will be affected."""
        protocol = self.currentProtocol
        #print "update docks", protocol.name()
        #print "  devices:", protocol.enabledDevices()
        
        ## (un)hide docks as needed
        for d in self.docks:
            if devNames is not None and d not in devNames:
                continue
            #print "  check", d
            if self.docks[d] is None:
                continue
            if d not in protocol.enabledDevices():
                #print "  hide", d
                self.hideDock(d)
            else:
                #print "  show", d
                self.showDock(d)
            
        ## Create docks that don't exist
        #pdb.set_trace()
        for d in protocol.enabledDevices():
            if devNames is not None and d not in devNames:
                continue
            
            if d not in self.docks:
                if d not in self.manager.listDevices():
                    continue
                self.docks[d] = None  ## Instantiate to prevent endless loops!
                #print "  Create", d
                try:
                    dev = self.manager.getDevice(d)
                    dw = dev.protocolInterface(self)
                except:
                    printExc("Error while creating dock '%s':" % d)
                    del self.docks[d]
                    
                if d in self.docks:
                    dock = QtGui.QDockWidget(d)
                    dock.setFeatures(dock.AllDockWidgetFeatures)
                    dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea|QtCore.Qt.TopDockWidgetArea)
                    dock.setObjectName(d)
                    dock.setWidget(dw)
                    dock.setAutoFillBackground(True)
                    dw.setSizePolicy(QtGui.QSizePolicy.Expanding, QtGui.QSizePolicy.Expanding)
                    self.docks[d] = dock
                    self.win.addDockWidget(QtCore.Qt.BottomDockWidgetArea, dock)
                    #QtCore.QObject.connect(dock.widget(), QtCore.SIGNAL('sequenceChanged'), self.updateSeqParams)
                    dock.widget().sigSequenceChanged.connect(self.updateSeqParams)
                    self.updateSeqParams(d)
                    #dock.setMinimumWidth(3000)
                    #dock.setMinimumWidth(0)
        
        
    def clearDocks(self):
        for d in self.docks:
            try:
                #print "request dock %s quit" % d
                self.docks[d].widget().quit()
            except:
                printExc("Error while requesting dock '%s' quit:"% d)
            try:
                self.win.removeDockWidget(self.docks[d])
                self.docks[d].close()
                #sip.delete(self.docks[d])
            except:
                printExc("Error while closing dock '%s':" % d)
        self.docks = {}

        for d in self.analysisDocks.keys()[:]:
            self.removeAnalysisDock(d)

        self.ui.sequenceParamList.clear()
        
        ## now's a good time to free up some memory.
        QtGui.QApplication.instance().processEvents()
        gc.collect()
                
        
    def quit(self):
        self.stopSingle()
        self.clearDocks()
        Module.quit(self)

    #def protParamsChanged(self):
        #self.currentProtocol.conf = self.protoStateGroup.state()
        ##self.currentProtocol.conf['duration'] = self.ui.protoDurationSpin.value()
        ##self.currentProtocol.conf['continuous'] = self.ui.protoContinuousCheck.isChecked()
        ##self.currentProtocol.conf['cycleTime'] = self.ui.seqCycleTimeSpin.value()
        ##self.currentIsModified(True)
        
    #def currentIsModified(self, v):
        ### Inform the module whether the current protocol is modified from its stored state
        #self.currentProtocol.modified = v
        #if (not v) or (self.currentProtocol.fileName is not None):
            #self.ui.saveProtocolBtn.setEnabled(v)
        
    def newProtocol(self):
        self.stopSingle()
        
        ## Remove all docks
        self.clearDocks()
        
        ## Create new empty protocol object
        self.currentProtocol = Protocol(self)
        
        self.protoStateGroup.setState({
            'continuous': False,
            'duration': 0.2,
            'leadTime': 0.01,
            'loop': False,
            'loopCycleTime': 0.3,
            'cycleTime': 0.3,
            'repetitions': 0
        })
        
        #self.currentProtocol.conf = self.protoStateGroup.state()
        
        ## Clear extra devices in dev list
        self.updateDeviceList()
        
        #self.updateProtParams()
        
        ## Clear sequence parameters, disable sequence dock
        self.updateSeqParams()
        
        #self.ui.currentProtocolLabel.setText('[ new ]')
        
        #self.ui.saveProtocolBtn.setEnabled(False)
        #self.currentIsModified(False)
        
        
    
    #def updateProtParams(self, prot=None):
        #if prot is None:
            #prot = self.currentProtocol
            
        #self.protoStateGroup.setState(prot.conf)
        ##self.ui.protoDurationSpin.setValue(prot.conf['duration'])
        ##if 'cycleTime' in prot.conf:
            ##self.ui.seqCycleTimeSpin.setValue(prot.conf['cycleTime'])
        ##if prot.conf['continuous']:
            ##self.ui.protoContinuousCheck.setCheckState(QtCore.Qt.Checked)
        ##else:
            ##self.ui.protoContinuousCheck.setCheckState(QtCore.Qt.Unchecked)
    
    #def getSelectedFileName(self):
        #"""Return the file name of the selected protocol"""
        #sel = list(self.ui.protocolList.selectedIndexes())
        #if len(sel) == 1:
            #index = sel[0]
        #else:
            #raise Exception("Can not load--%d items selected" % len(sel))
        #return self.protocolList.getFileName(index)
    
    #def loadProtocol(self, index=None):
    def loadProtocol(self, handle):
        try:
            QtGui.QApplication.setOverrideCursor(QtGui.QCursor(QtCore.Qt.WaitCursor))
            self.stopSingle()
            
            ## Determine selected item
            #if index is None:
                #sel = list(self.ui.protocolList.selectedIndexes())
                #if len(sel) == 1:
                    #index = sel[0]
                #else:
                    #raise Exception("Can not load--%d items selected" % len(sel))
                
            #fn = self.protocolList.getFileName(index)
            fn = handle.name()
            
            ## Remove all docks
            self.clearDocks()
            
            ## Create protocol object from requested file
            prot = Protocol(self, fileName=fn)
            ## Set current protocol
            self.currentProtocol = prot
            
            #print "Docks cleared."
            
            ## Update protocol parameters
            self.protoStateGroup.setState(prot.conf['conf'])
            #self.updateProtParams(prot)
            
            ## update dev list
            self.updateDeviceList()
            
            ## Update sequence parameters, dis/enable sequence dock
            
            ## Create new docks
            
            self.updateDeviceDocks()
            
            
            ## Configure docks
            for d in prot.devices:
                if d in self.docks:
                    try:
                        self.docks[d].widget().restoreState(prot.devices[d])
                    except:
                        printExc("Error while loading protocol dock:")
    
            ## create and configure analysis docks
            if 'analysis' in prot.conf:
                for k in prot.conf['analysis']:
                    try:
                        self.createAnalysisDock(k)
                        conf = prot.conf['analysis'][k]
                        self.analysisDocks[k].widget().restoreState(conf)
                    except:
                        printExc("Error while loading analysis dock:")
                        
    
            ## Load sequence parameter state (must be done after docks have loaded)
            self.ui.sequenceParamList.loadState(prot.conf['params'])
            self.updateSeqParams('protocol')
            
            ## Configure dock positions
            winState = prot.conf['windowState']
            if winState is not None:
                self.win.restoreState(winState)
                
            
                
                
            #pn = fn.replace(self.protocolList.baseDir, '')
            #self.ui.currentProtocolLabel.setText(pn)
            #self.ui.saveProtocolBtn.setEnabled(True)
            #self.currentIsModified(False)
        finally:
            QtGui.QApplication.restoreOverrideCursor()
            
            
    def saveProtocol(self, fileHandle=None):
        ## Write protocol config to file
        self.currentProtocol.write(fileHandle.name())

    def testSingleClicked(self):
        self.testSingle()

    def runSingleClicked(self):
        self.runSingle(store=True)
        
    def testSingle(self):
        self.runSingle(store=False)
    
    def runSingle(self, store=True):
        
        if self.protoStateGroup.state()['loop']:
            self.loopEnabled = True
        #print "RunSingle"
        #if self.taskThread.isRunning():
            #import traceback
            #traceback.print_stack()
            #print "Task already running."

        self.lastProtoTime = ptime.time()
        ## Disable all start buttons
        self.enableStartBtns(False)
        
        ## Set storage dir
        try:
            if store:
                currentDir = self.manager.getCurrentDir()
                name = self.currentProtocol.name()
                if name is None:
                    name = 'protocol'
                info = self.protocolInfo()
                info['dirType'] = 'Protocol'
                ## Create storage directory with all information about the protocol to be executed
                dh = currentDir.mkdir(name, autoIncrement=True, info=info)
            else:
                dh = None

            ## Tell devices to prepare for protocol start.
            for d in self.currentProtocol.devices:
                if self.currentProtocol.deviceEnabled(d):
                    self.docks[d].widget().prepareProtocolStart()
            
            ## Generate executable conf from protocol object
            prot = self.generateProtocol(dh)
            #print prot
            #self.emit(QtCore.SIGNAL('protocolStarted'), {})
            self.sigProtocolStarted.emit({})
            #print "runSingle: Starting taskThread.."
            self.taskThread.startProtocol(prot)
            #print "runSingle: taskThreadStarted"
        except:
            exc = sys.exc_info()
            self.enableStartBtns(True)
            self.loopEnabled = False
            #print "Error starting protocol. "
            raise HelpfulException("Error starting protocol:", exc=exc)      
   
    def runSequenceClicked(self):
        self.runSequence(store=True)
        
   
    def testSequence(self):
        self.runSequence(store=False)
       
    def runSequence(self, store=True):
        ## Disable all start buttons
        self.enableStartBtns(False)
        
        ## Find all top-level items in the sequence parameter list
        try:
            ## make sure all devices are reporting their correct sequence lists
            
            
            items = self.ui.sequenceParamList.listParams()
            #for i in self.ui.sequenceParamList.topLevelItems:
                #items.append(i)
            ## Generate parameter space
            params = OrderedDict()
            paramInds = OrderedDict()
            linkedParams = {}
            pLen = 1
            for i in items:
                key = i[:2]
                params[key] = i[2]
                paramInds[key] = range(len(i[2]))
                pLen *= len(i[2])
                linkedParams[key] = i[3]
                
            ## Set storage dir
            if store:
                currentDir = self.manager.getCurrentDir()
                name = self.currentProtocol.name()
                if name is None:
                    name = 'protocol'
                info = self.protocolInfo(params)
                info['dirType'] = 'ProtocolSequence'
                dh = currentDir.mkdir(name, autoIncrement=True, info=info)
            else:
                dh = None
                
            ## Tell devices to prepare for protocol start.
            for d in self.currentProtocol.devices:
                if self.currentProtocol.deviceEnabled(d):
                    self.docks[d].widget().prepareProtocolStart()
                    
            #print params, linkedParams
            ## Generate the complete array of command structures. This can take a long time, so we start a progress dialog.
            with pg.ProgressDialog("Generating protocol commands..", 0, pLen) as progressDlg:
                #progressDlg.setMinimumDuration(500)  ## If this takes less than 500ms, progress dialog never appears.
                self.lastQtProcessTime = ptime.time()
                prot = runSequence(lambda p: self.generateProtocol(dh, p, progressDlg), paramInds, paramInds.keys(), linkedParams=linkedParams)
                #progressDlg.setValue(pLen)
            
            #print "==========Sequence Protocol=============="
            #print prot
            #self.emit(QtCore.SIGNAL('protocolStarted'), {})
            self.sigProtocolStarted.emit({})
            logMsg('Started %s protocol sequence of length %i' %(self.currentProtocol.name(),pLen), importance=6)
            #print 'PR protocol positions:
            self.taskThread.startProtocol(prot, paramInds)
            
        except:
            self.enableStartBtns(True)

            raise
        
    def generateProtocol(self, dh, params=None, progressDlg=None):
        #prof = Profiler("Generate Protocol: %s" % str(params))
        ## params should be in the form {(dev, param): value, ...}
        ## Generate executable conf from protocol object
        #prot = {'protocol': {
            #'duration': self.currentProtocol.conf['duration'], 
            #'storeData': store,
            #'mode': 'single',
            #'name': self.currentProtocol.fileName,
            #'cycleTime': self.currentProtocol.conf['cycleTime'], 
        #}}
        #print "generate:", params
        ## Never put {} in the function signature
        if params is None:
            params = {}
        prot = {'protocol': self.protoStateGroup.state()}
        #prof.mark('protocol state')
        store = (dh is not None)
        prot['protocol']['storeData'] = store
        if store:
            if params != {}:
                name = '_'.join(map(lambda i: '%03d'%i, params.values()))
                #print "mkdir", name
                info = params.copy()
                info['dirType'] = 'Protocol'
                dh1 = dh.mkdir(name, info=info)
                #prof.mark('create storage dir')
            else:
                dh1 = dh
            prot['protocol']['storageDir'] = dh1
        #prof.mark('selected storage dir.')
        prot['protocol']['name'] = self.currentProtocol.fileName
        
        for d in self.currentProtocol.devices:
            if self.currentProtocol.deviceEnabled(d):
                ## select out just the parameters needed for this device
                p = dict([(i[1], params[i]) for i in params.keys() if i[0] == d])
                ## Ask the device to generate its protocol command
                if d not in self.docks:
                    raise HelpfulException("The device '%s' currently has no dock loaded." % d,
                                           reasons=[
                                               "This device name does not exist in the system's configuration",
                                               "There was an error when creating the device at program startup",
                                               ],
                                           tags={},
                                           importance=8,

                                           docSections=['userGuide/modules/ProtocolRunner/loadingNonexistentDevices']
                                           )
                prot[d] = self.docks[d].widget().generateProtocol(p)
                #prof.mark("get protocol from %s" % d)
        #print prot['protocol']['storageDir'].name()
        
        if progressDlg is not None:
            progressDlg.setValue(progressDlg.value()+1)
            ## only do UI updates every 1 sec.
            now = ptime.time()
            if now - self.lastQtProcessTime > 1.0:
                self.lastQtProcessTime = now
                QtGui.QApplication.processEvents()
            if progressDlg.wasCanceled():
                raise Exception("Target sequence computation canceled by user.")
        #prof.mark('done')
        return prot
    
    def protocolInfo(self, params=None):
        """
        Generate a complete description of the protocol.
        This data is stored with the results of each protocol run.
        """
        conf = self.saveState()
        del conf['windowState']
        #del conf['params']
        conf['params'] = self.ui.sequenceParamList.listParams()
        
        devs = {}
        ## store individual dock states
        for d in self.docks:
            if self.currentProtocol.deviceEnabled(d):
                devs[d] = self.docks[d].widget().describe(params=params)
        
        ## Remove unused devices before writing
        #rem = [d for d in devs if not self.deviceEnabled(d)]
        #for d in rem:
            #del devs[d]
        desc = {'protocol': conf, 'devices': devs}  #, 'winState': self.winState}

        if params is not None:
            desc['sequenceParams'] = params
        return desc
    
    
    def enableStartBtns(self, v):
        btns = [self.ui.testSingleBtn, self.ui.runProtocolBtn, self.ui.testSequenceBtn, self.ui.runSequenceBtn]
        for b in btns:
            b.setEnabled(v)
            
    def taskThreadStopped(self):
        #self.emit(QtCore.SIGNAL('protocolFinished'))
        self.sigProtocolFinished.emit()
        if not self.loopEnabled:   ## what if we quit due to error?
            self.enableStartBtns(True)
    
    def taskErrored(self):
        self.enableStartBtns(True)
            
    def taskThreadPaused(self):
        #self.emit(QtCore.SIGNAL('protocolPaused'))
        self.sigProtocolPaused.emit()
            
    def stopSingle(self):
        self.loopEnabled = False
        if self.taskThread.isRunning():
            self.taskThread.abort()
        self.ui.pauseSequenceBtn.setChecked(False)
        
    def stopSequence(self):
        self.loopEnabled = False
        if self.taskThread.isRunning():
            self.taskThread.stop()
        self.ui.pauseSequenceBtn.setChecked(False)
    
    def pauseSequence(self, pause):
        self.taskThread.pause(pause)
        
    def taskStarted(self, params):
        cur = 'Current iteration:\n'
        plist = self.ui.sequenceParamList.listParams()
        try:
            nums = [str(params[p[:2]]) for p in plist]
        except:
            nums = []
        cur += ',  '.join(nums)
        self.ui.seqCurrentLabel.setText(cur)
        #self.emit(QtCore.SIGNAL('taskStarted'), params)
        self.sigTaskStarted.emit(params)
        
    
    def handleFrame(self, frame):
        
        ## Request each device handles its own data
        ## Note that this is only used to display results; data storage is handled by Manager and the individual devices.
        #print "got frame", frame
        prof = Profiler('ProtocolRunner.handleFrame', disabled=True)
        for d in frame['result']:
            try:
                if d != 'protocol':
                    self.docks[d].widget().handleResult(frame['result'][d], frame['params'])
                    prof.mark('finished %s' % d)
            except:
                printExc("Error while handling result from device '%s'" % d)
                
        #self.emit(QtCore.SIGNAL('newFrame'), frame)
        self.sigNewFrame.emit(frame)
        prof.mark('emit newFrame')
                
        ## If this is a single-mode protocol and looping is turned on, schedule the next run
        if self.loopEnabled:
            ct = self.protoStateGroup.state()['loopCycleTime']
            t = max(0, ct - (ptime.time() - self.lastProtoTime))
            QtCore.QTimer.singleShot(int(t*1000.), self.loop)
        prof.finish()
            
    def loop(self):
        """Run one iteration when in loop mode"""
        if not self.loopEnabled:
            self.enableStartBtns(True)
            return

        if self.taskThread.isRunning():  ## If a protocol is still running, delay 10ms and try again
            QtCore.QTimer.singleShot(10, self.loop)
        else:
            self.testSingle()

    def saveState(self):
        ## Returns a description of the current window state -- dock positions, parameter list order, and analysis dock states.
        conf = self.protoStateGroup.state()
        
        ## store window state
        ws = str(self.win.saveState().toPercentEncoding())
        #self.winState = ws
        
        ## store parameter order/state
        params = self.ui.sequenceParamList.saveState()
        
        adocks = {}
        for d in self.analysisDocks:
            adocks[d] = self.analysisDocks[d].widget().saveState()
        
        return {'conf': conf, 'params': params, 'windowState': ws, 'analysis': adocks}
        

    
class Protocol:
    def __init__(self, ui, fileName=None):
        self.ui = ui
        
        if fileName is not None:
            self.fileName = fileName
            conf = configfile.readConfigFile(fileName)
            if 'protocol' not in conf:
                self.conf = conf
            else:
                self.conf = conf['protocol']
            if 'params' not in self.conf:
                self.conf['params'] = []
                
            if 'winState' in conf:
                self.conf['windowState'] = conf['winState']
            self.conf['windowState'] = QtCore.QByteArray.fromPercentEncoding(self.conf['windowState'])
                
            #self.params = conf['params']
            self.devices = conf['devices']
            #self.winState = conf['winState']
            self.enabled = self.devices.keys()
        else:
            self.fileName = None
            #self.conf = {
                #'devices': {}, 
                #'duration': 0.2, 
                #'continuous': False, 
                #'cycleTime': 0.0
            #}
            self.enabled = []
            self.conf = {}
            self.devices = {}
            self.winState = None

    
    def deviceEnabled(self, dev):
        return dev in self.enabled
        
        
    #def updateFromUi(self):
        
        
    def write(self, fileName=None):
        ## Write this protocol to a file. Called by ProtocolRunner.saveProtocol()
        info = self.saveState()
                
        if fileName is None:
            if self.fileName is None:
                raise Exception("Can not write protocol--no file name specified")
            fileName = self.fileName
        self.fileName = fileName
        configfile.writeConfigFile(info, fileName)
        
    def name(self):
        if self.fileName is None:
            return None
        return os.path.split(self.fileName)[1]
    
    def saveState(self):
        ## Generate a description of this protocol. The description 
        ## can be used to save/reload the protocol (calls saveState on all devices). 
        
        self.conf = self.ui.saveState()
        
        ## store window state
        #ws = str(self.ui.win.saveState().toPercentEncoding())
        #self.winState = ws
        
        ## store individual dock states
        for d in self.ui.docks:
            if self.deviceEnabled(d):
                self.devices[d] = self.ui.docks[d].widget().saveState()
        #self.updateFromUi()
        
        conf = self.conf.copy()
        devs = self.devices.copy()
        
        ## Remove unused devices before writing
        rem = [d for d in devs if not self.deviceEnabled(d)]
        for d in rem:
            del devs[d]
        return {'protocol': conf, 'devices': devs}  #, 'winState': self.winState}

        
    
    def enabledDevices(self):
        return self.enabled[:]
        
    def removeDevice(self, dev):
        if dev in self.enabled:
            self.enabled.remove(dev)
        
    def addDevice(self, dev):
        if dev not in self.devices:
            self.devices[dev] = {}
        if dev not in self.enabled:
            self.enabled.append(dev)
            
    def renameDevice(self, oldName, newName):
        if oldName not in self.conf['devices']:
            return
        self.devices[newName] = self.devices[oldName]
        del self.devices[oldName]
        if oldName in self.enabled:
            self.enabled.append(newName)
            self.enabled.remove(oldName)
        else:
            if newName in self.enabled:
                self.enabled.remove(newName)
            
        
        
class TaskThread(QtCore.QThread):
    
    sigPaused = QtCore.Signal()
    sigNewFrame = QtCore.Signal(object)
    sigExitFromError = QtCore.Signal()
    sigTaskStarted = QtCore.Signal(object)
    
    def __init__(self, ui):
        QtCore.QThread.__init__(self)
        self.ui = ui
        self.dm = self.ui.manager
        self.lock = Mutex(QtCore.QMutex.Recursive)
        self.stopThread = True
        self.abortThread = False
        self.paused = False
                
    def startProtocol(self, protocol, paramSpace=None):
        #print "TaskThread:startProtocol", self.lock.depth(), self.lock
        with MutexLocker(self.lock):
            #print "TaskThread:startProtocol got lock", self.lock.depth(), "    tracebacks follow:\n==========="
            #print "\n\n".join(self.lock.traceback())
            #print "======================"
            while self.isRunning():
                #l.unlock()
                raise Exception("Already running another protocol")
            self.protocol = protocol
            self.paramSpace = paramSpace
            self.lastRunTime = None
            #l.unlock()
            #print "TaskThread:startProtocol starting..", self.lock.depth()
            self.start() ### causes self.run() to be called from somewhere in C code
            #name = '' if protocol.fileName is None else protocol.fileName
            logMsg("Protocol started.", importance=1)
            #print "TaskThread:startProtocol started", self.lock.depth()
    
    def pause(self, pause):
        with MutexLocker(self.lock):
            self.paused = pause
                
    def run(self):
        self.objs = None
        #print "TaskThread:run()"
        try:
            #print "TaskThread:run   waiting for lock..", self.lock.depth()
            with MutexLocker(self.lock):
                #print "TaskThread:run   got lock."
                self.stopThread = False
                self.abortThread = False
            
            if self.paramSpace is None:
                try:
                    self.runOnce()
                except Exception, e:
                    if e.args[0] != 'stop':
                        raise
            else:
                #runner = SequenceRunner(self.paramSpace, self.paramSpace.keys())
                #runner.setEndFuncs([]*len(self.paramSpace) + [self.checkStop])
                #result = runner.start(self.runOnce)
                    
                runSequence(self.runOnce, self.paramSpace, self.paramSpace.keys())
            
        except:
            self.protocol = None  ## free up this memory
            self.paramSpace = None
            printExc("Error in protocol thread, exiting.")
            #self.emit(QtCore.SIGNAL('exitFromError'))
            self.sigExitFromError.emit()
        #finally:
            #self.emit(QtCore.SIGNAL("protocolFinished()"))
        #print "TaskThread:run() finished"
                    
    def runOnce(self, params=None):
        #print "TaskThread:runOnce"
        prof = Profiler("ProtocolRunner.TaskThread.runOnce", disabled=True, delayed=False)
        startTime = ptime.time()
        if params is None:
            params = {}
        with MutexLocker(self.lock) as l:
            l.unlock()
        
            ## Select correct command to execute
            cmd = self.protocol
            #print "Sequence array:", cmd.shape, cmd.infoCopy()
            if params is not None:
                for p in params:
                    #print "Selecting %s: %s from sequence array" % (str(p), str(params[p]))
                    cmd = cmd[p: params[p]]
            prof.mark('select command')        
            #print "Protocol:", cmd
                    
            ## Wait before starting if we've already run too recently
            #print "sleep until next run time..", ptime.time(), self.lastRunTime, cmd['protocol']['cycleTime']
            while (self.lastRunTime is not None) and (ptime.time() < self.lastRunTime + cmd['protocol']['cycleTime']):
                l.relock()
                if self.abortThread or self.stopThread:
                    l.unlock()
                    #print "Protocol run aborted by user"
                    return
                l.unlock()
                time.sleep(1e-3)
            #print "slept until", ptime.time()
            prof.mark('sleep')
            
            # If paused, hang here for a bit.
            emitSig = True
            while True:
                l.relock()
                if self.abortThread or self.stopThread:
                    l.unlock()
                    return
                pause = self.paused
                l.unlock()
                if not pause:
                    break
                if emitSig:
                    emitSig = False
                    #self.emit(QtCore.SIGNAL('paused'))
                    self.sigPaused.emit()
                time.sleep(10e-3)
            
            prof.mark('pause')
            
            #print "BEFORE:\n", cmd
            if type(cmd) is not dict:
                print "========= ProtocolRunner.runOnce cmd: =================="
                print cmd
                print "========= ProtocolRunner.runOnce params: =================="
                print "Params:", params
                print "==========================="
                raise Exception("ProtocolRunner.runOnce failed to generate a proper command structure. Object type was '%s', should have been 'dict'." % type(cmd))
                
            
            task = self.dm.createTask(cmd)
            prof.mark('create task')
            
            self.lastRunTime = ptime.time()
            #self.emit(QtCore.SIGNAL('taskStarted'), params)
            self.sigTaskStarted.emit(params)
            
            try:
                task.execute(block=False)
                prof.mark('execute')
            except:
                try:
                    task.stop(abort=True)
                except:
                    pass
                printExc("\nError starting protocol:")
                exc = sys.exc_info()
                raise HelpfulException("\nError starting protocol:", exc)
            
            prof.mark('start task')
            ### Do not put code outside of these try: blocks; may cause device lockup
            
            try:
                ## wait for finish, watch for abort requests
                while True:
                    if task.isDone():
                        prof.mark('task done')
                        break
                    l.relock()
                    if self.abortThread:
                        l.unlock()
                        task.stop(abort=True)
                        return
                    l.unlock()
                    time.sleep(1e-3)
                    
                result = task.getResult()
            except:
                ## Make sure the task is fully stopped if there was a failure at any point.
                #printExc("\nError during protocol execution:")
                print "\nStopping task.."
                task.stop(abort=True)
                print ""
                raise HelpfulException("\nError during protocol execution:", sys.exc_info())
            #print "\nAFTER:\n", cmd
            prof.mark('getResult')
            
        frame = {'params': params, 'cmd': cmd, 'result': result}
        #self.emit(QtCore.SIGNAL('newFrame'), frame)
        self.sigNewFrame.emit(frame)
        prof.mark('emit newFrame')
        if self.stopThread:
            raise Exception('stop', result)
        #print "Total run time: %gms" % ((ptime.time() - startTime) * 1000 )
        
        ## Give everyone else a chance to catch up
        QtCore.QThread.yieldCurrentThread()
        prof.mark('yield')
        prof.finish()
        
    def checkStop(self):
        with MutexLocker(self.lock):
            if self.stopThread:
                raise Exception('stop')
        
        
    def stop(self, block=False):
        with MutexLocker(self.lock):
            self.stopThread = True
        if block:
            if not self.wait(10000):
                raise Exception("Timed out while waiting for thread exit!")
            
    def abort(self):
        with MutexLocker(self.lock):
            self.abortThread = True



