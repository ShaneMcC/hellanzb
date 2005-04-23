"""

NZBLeecher - Downloads article segments from an NZBQueue, then passes them off for
decoding

The NZBLeecher module (ArticleDecoder, NZBModel etc) is a rewrite of pynewsleecher by
Freddie (freddie@madcowdisease.org) utilizing the twisted framework

(c) Copyright 2005 Philip Jenvey, Ben Bangert
[See end of file]
"""
import os, time
from twisted.internet import reactor
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineReceiver
from twisted.protocols.nntp import NNTPClient, extractCode
from twisted.protocols.policies import TimeoutMixin
from twisted.python import log
from Hellanzb.Core import shutdown
from Hellanzb.Log import *
from Hellanzb.Logging import LogOutputStream
from Hellanzb.Util import rtruncate
from Hellanzb.NZBLeecher.ArticleDecoder import decode
from Hellanzb.NZBLeecher.NZBModel import NZBQueue
from Queue import Empty

__id__ = '$Id$'

def initNZBLeecher():
    """ Init """
    # Direct twisted log output to the debug level
    fileStream = LogOutputStream(debug)
    log.startLogging(fileStream)

    # Create the one and only download queue
    Hellanzb.queue = NZBQueue()
    
    # The NZBLeecherFactories
    Hellanzb.nsfs = []
    Hellanzb.totalSpeed = 0

    # this class handles updating statistics via the SCROLL level (the UI)
    Hellanzb.scroller = NZBLeecherStatLog()

    startNZBLeecher()

def startNZBLeecher():
    """ gogogo """
    defaultAntiIdle = 7 * 60
    
    connectionCount = 0
    for serverId, serverInfo in Hellanzb.SERVERS.iteritems():
        hosts = serverInfo['hosts']
        connections = int(serverInfo['connections'])
        info('(' + serverId + ') Connecting... ', appendLF = False)

        for host in hosts:
            if serverInfo.has_key('antiIdle') and serverInfo['antiIdle'] != None and \
                    serverInfo['antiIdle'] != '':
                antiIdle = serverInfo['antiIdle']
            else:
                antiIdle = defaultAntiIdle

            nsf = NZBLeecherFactory(serverInfo['username'], serverInfo['password'],
                                    antiIdle)
            Hellanzb.nsfs.append(nsf)

            host, port = host.split(':')
            for connection in range(connections):
                if serverInfo.has_key('bindTo') and serverInfo['bindTo'] != None and \
                        serverInfo['bindTo'] != '':
                    reactor.connectTCP(host, int(port), nsf,
                                       bindAddress = serverInfo['bindTo'])
                else:
                    reactor.connectTCP(host, int(port), nsf)
                connectionCount += 1

    if connectionCount == 1:
        info('opened ' + str(connectionCount) + ' connection.')
    else:
        info('opened ' + str(connectionCount) + ' connections.')
        
    Hellanzb.scroller.maxCount = connectionCount

    # Allocate only one thread, just for decoding
    reactor.suggestThreadPoolSize(1)
    
    reactor.run()

class NZBLeecherFactory(ReconnectingClientFactory):

    def __init__(self, username, password, antiIdleTimeout):
        self.username = username
        self.password = password
        self.antiIdleTimeout = antiIdleTimeout

        # FIXME: don't think these are actually used
        #self.totalStartTime = None
        self.totalReadBytes = 0
        self.totalDownloadedFiles = 0

        # statistics for the current session (sessions end when we stop downloading on all
        # ports). used for the more accurate total speeds shown in the UI
        self.sessionReadBytes = 0
        self.sessionSpeed = 0
        self.sessionStartTime = None
        
        # FIXME: idle the connection by: returning nothing, having a callLater handle an
        # idle call. whenever there's activity, we cancel the idle call and reschedule for
        # later
        self.clients = []

        from sets import Set
        self.activeClients = Set()

        # FIXME: factories need to know when we're idle (done downloading). then it can
        # turn the auto reconnect maxDelay up back to the default value (3600)
        #self.maxDelay = 5
        # turning this off for now -- but it might be useful for when usenet servers start
        # shitting themselves

    def buildProtocol(self, addr):
        p = NZBLeecher(self.username, self.password)
        p.factory = self
        p.timeOut = self.antiIdleTimeout
        
        # FIXME: Is it safe to maintain the clients in this list? no other twisted
        # examples do this. no twisted base factory classes seem to maintain this list
        self.clients.append(p)

        # FIXME: registerScrollingClient
        Hellanzb.scroller.size += 1
        return p

    def fetchNextNZBSegment(self):
        for p in self.clients:
            reactor.callLater(0, p.fetchNextNZBSegment)

class AntiIdleMixin(TimeoutMixin):
    """ policies.TimeoutMixin calls self.timeoutConnection after the connection has been idle
    too long. Anti-idling the connection involves the same operation, so we extend
    TimeoutMixin, anti-idle instead, and reset the timeout after anti-idling (to repeat
    the process -- unlike TimeoutMixin) """
    def antiIdleConnection(self):
        """ """
        raise NotImplementedError()

    def timeoutConnection(self):
        """ Called when the connection times out -- i.e. when we've been idle longer than the
        self.timeOut value """
        self.antiIdleConnection()

        # TimeoutMixin assumes we're done (timed out) after timeoutConnection. Since we're
        # still connected, we need to manually reset the timeout
        self.setTimeout(self.timeOut)

class NZBLeecher(NNTPClient, AntiIdleMixin):
    """ Extends twisted NNTPClient to download NZB segments from the queue, until the queue
    contents are exhausted """

    nextId = 0 # Id Pool
    
    def __init__(self, username, password):
        """ """
        NNTPClient.__init__(self)
        self.username = username
        self.password = password
        self.id = self.getNextId()

        # successful GROUP commands during this session
        self.activeGroups = []

        # current article (<segment>) we're dealing with
        self.currentSegment = None

        # staistics/for the ui
        self.downloadStartTime = None
        self.readBytes = 0

        self.myState = None

        self.isLoggedIn = False
        self.setReaderAfterLogin = False
            
        # How long we must be idle for in seconds until we send an anti idle request
        self.timeOut = 7 * 60

        # I'm not sure why this needs to be raised from the default value -- but we can
        # definitely get longer lines than LineReceiver expects
        self.MAX_LENGTH = 262144

        # Lameness -- these are from LineReceiver. Needed for the imported Twisted 2.0
        # dataReceieved
        self.line_mode = 1
        self.__buffer = ''
        self.delimiter = '\r\n'
        self.paused = False

    def authInfo(self):
        """ """
        self.sendLine('AUTHINFO USER ' + self.username)
        self._newState(None, self.authInfoFailed, self._authInfoUserResponse)

    def _authInfoUserResponse(self, (code, message)):
        """ """
        if code == 381:
            self.sendLine('AUTHINFO PASS ' + self.password)
            self._newState(None, self.authInfoFailed, self._authInfoPassResponse)
        else:
            self.authInfoFailed('%d %s' % (code, message))
        self._endState()

    def _authInfoPassResponse(self, (code, message)):
        """ """
        if code == 281:
            self.gotauthInfoOk('%d %s' % (code, message))
        else:
            self.authInfoFailed('%d %s' % (code, message))
        self._endState()

    def gotauthInfoOk(self, message):
        """ Override for notification when authInfo() action is successful """
        debug(self.getName() + ' AUTHINFO succeeded: ' + message)
        self.isLoggedIn = True

        # Reset the auto-reconnect delay
        self.factory.resetDelay()

        if self.setReaderAfterLogin:
            self.setReader()
        else:
            reactor.callLater(0, self.fetchNextNZBSegment)

    def authInfoFailed(self, err):
        "Override for notification when authInfoFailed() action fails"
        debug(self.getName() + ' AUTHINFO failed: ' + str(err))

    def connectionMade(self):
        NNTPClient.connectionMade(self)
        self.setTimeout(self.timeOut)

        # 'mode reader' is sometimes necessary to enable 'reader' mode.
        # However, the order in which 'mode reader' and 'authinfo' need to
        # arrive differs between some NNTP servers. Try to send
        # 'mode reader', and if it fails with an authorization failed
        # error, try again after sending authinfo.
        self.setReader()

    def connectionLost(self, reason):
        self.setTimeout(None)
        # FIXME: could put failed segments into a failed queue. connections that are
        # flaged as being fill servers would try to attempt to d/l the failed files. you
        # couldn't write a 0 byte file to disk in that case -- not until all fill servers
        # had tried downloading
        
        # ReconnectingClientFactory will pretend it wants to reconnect after we CTRL-C --
        # we'll quiet it by canceling it
        if Hellanzb.shutdown:
            self.factory.stopTrying()

        NNTPClient.connectionLost(self) # calls self.factory.clientConnectionLost(self, reason)

        if self.currentSegment != None:
            if self.currentSegment in Hellanzb.scroller.segments:
                Hellanzb.scroller.segments.remove(self.currentSegment)
            # twisted doesn't reconnect our same client connections, we have to pitch
            # stuff back into the queue that hasn't finished before the connectionLost
            # occurred
            Hellanzb.queue.put((Hellanzb.queue.NZB_CONTENT_P, self.currentSegment))
        
        # Continue being quiet about things if we're shutting down
        if not Hellanzb.shutdown:
            debug(self.getName() + ' lost connection: ' + str(reason))

        self.activeGroups = []
        self.factory.clients.remove(self)
        Hellanzb.scroller.size -= 1
        self.isLoggedIn = False
        self.setReaderAfterLogin = False

    def setReader(self):
        """ Tell the server we're a news reading client (MODE READER) """
        self.sendLine('MODE READER')
        self._newState(None, self.setReaderFailed, self.setReaderModeResponse)

    def setReaderModeResponse(self, (code, message)):
        if code in (200, 201):
            self.setReaderSuccess()
        else:
            self.setReaderFailed((code, message))
        self._endState()
        
    def setReaderSuccess(self):
        """ """
        debug(self.getName() + ' MODE READER successful')
        if self.setReaderAfterLogin:
            reactor.callLater(0, self.fetchNextNZBSegment)
        else:
            self.authInfo()
        
    def setReaderFailed(self, err):
        """ If the MODE READER failed prior to login, this server probably only accepts it after
        login """
        if not self.isLoggedIn:
            self.setReaderAfterLogin = True
            self.authInfo()
        else:
            debug(self.getName() + 'MODE READER failed, err: ' + str(err))
        
    def fetchNextNZBSegment(self):
        """ Pop nzb article from the queue, and attempt to retrieve it if it hasn't already been
        retrieved"""
        if self.currentSegment is None:
            if self not in self.factory.activeClients:
                if len(self.factory.activeClients) == 0:
                    self.factory.sessionStartTime = time.time()
                self.factory.activeClients.add(self)
            try:
                nextSegment = Hellanzb.queue.get_nowait()
                #while not nextSegment.needsDownload(threadRealNameWork = True):
                    # FIXME: could do a segment.fileDone(). would add segment to
                    # nzbFile.finishedSegments list (if it isn't already
                    # there). needsDownload() could call this if it finds a match on the
                    # filesystem. easy way to maintain what/when is done all the time (i
                    # think)

                    # FIXME: we should maybe notify the user we skipped a file that wasn't
                    # the expected size. passing an argument to needsDownload could do the
                    # work for us
                    
               #     nextSegment.nzbFile.totalSkippedBytes += nextSegment.bytes
                    # TODO: decrement the skippedBytes from the Queue (queue should
                    # maintain the byte count down to the segment intstead of file)
                    
               #     debug(self.getName() + ' SKIPPING segment: ' + nextSegment.getTempFileName() + \
               #           ' subject: ' + nextSegment.nzbFile.subject)
               #     nextSegment = Hellanzb.queue.get_nowait()

                self.currentSegment = nextSegment
                if self.currentSegment.nzbFile.showFilename == None:
                    if self.currentSegment.nzbFile.filename == None:
                        self.currentSegment.nzbFile.showFilenameIsTemp = True
                    self.currentSegment.nzbFile.showFilename = os.path.basename(self.currentSegment.nzbFile.getDestination())
            except Empty:
                self.factory.activeClients.remove(self)
                if len(self.factory.activeClients) == 0:
                    self.factory.sessionReadBytes = 0
                    self.factory.sessionSpeed = 0
                    self.factory.sessionStartTime = None

                totalActiveClients = 0
                for nsf in Hellanzb.nsfs:
                    totalActiveClients += len(nsf.activeClients)
                if totalActiveClients == 0:
                    Hellanzb.totalSpeed = 0
                    Hellanzb.scroller.currentLog = None
                    
                # FIXME: 'Transferred %s in %.1fs at %.1fKB/s' % (ldb, dur, speed)
                                    
                return

        # Change group
        #gotActiveGroup = False
        for i in xrange(len(self.currentSegment.nzbFile.groups)):
            group = str(self.currentSegment.nzbFile.groups[i])

            # NOTE: we could get away with activating only one of the groups instead of
            # all
            if group not in self.activeGroups:
                debug(self.getName() + ' getting GROUP: ' + group)
                self.fetchGroup(group)
                return
            #else:
            #    gotActiveGroup = True

        #if not gotActiveGroup:
            # FIXME: prefix with segment name
        #    Hellanzb.scroller.prefixScroll('No valid group found!')
        #    Hellanzb.scroller.updateLog(logNow = True)
            
        debug(self.getName() + ' getting BODY: <' + self.currentSegment.messageId + '> ' + \
              self.currentSegment.getDestination())
        reactor.callLater(0, self.fetchBody, str(self.currentSegment.messageId))
        
    def fetchBody(self, index):
        """ """
        self.myState = 'BODY'
        start = time.time()
        if self.currentSegment.nzbFile.downloadStartTime == None:
            self.currentSegment.nzbFile.downloadStartTime = start
        self.downloadStartTime = start
        
        Hellanzb.scroller.segments.append(self.currentSegment)

        #reactor.callLater(0, NNTPClient.fetchBody, self, '<' + index + '>')
        NNTPClient.fetchBody(self, '<' + index + '>')

    def getName(self):
        """ Return the name of this NZBLeecher instance """
        return self.__class__.__name__ + '[' + str(self.id) + ']'

    def getNextId(self):
        id = NZBLeecher.nextId
        NZBLeecher.nextId += 1
        return id

    def gotBody(self, body):
        """ Queue the article body for decoding and continue fetching the next article """
        #debug(self.getName() + ' got BODY: ' + ' <' + self.currentSegment.messageId + '> ' + \
        #      self.currentSegment.getDestination() + ' lines: ' + str(len(body)) + ' expected size: ' + \
        #      str(self.currentSegment.bytes))
        debug(self.getName() + ' got BODY: ' + ' <' + self.currentSegment.messageId + '> ' + \
              self.currentSegment.getDestination())

        #reactor.callLater(0, self.processBodyAndContinue, body)
        self.processBodyAndContinue(body)
        
    def gotBodyFailed(self, err):
        """ Handle a failure of the BODY command. Ensure the failed segment gets a 0 byte file
        written to the filesystem when this occurs """
        debug(self.getName() + ' got BODY FAILED, error: ' + str(err) + ' for messageId: <' + \
              self.currentSegment.messageId + '> ' + self.currentSegment.getDestination() + \
              ' expected size: ' + str(self.currentSegment.bytes))
        
        code = extractCode(err)
        if code is not None and code in ('423', '430'):
            # FIXME: show filename and segment number
            Hellanzb.scroller.prefixScroll(self.currentSegment.showFilename + ' Article is missing!')
            Hellanzb.scroller.updateLog(logNow = True)
        
        #reactor.callLater(0, self.processBodyAndContinue, '')
        self.processBodyAndContinue('')

    def processBodyAndContinue(self, articleData):
        """ Defer decoding of the specified articleData of the currentSegment, reset our state and
        continue fetching the next queued segment """
        self.myState = None

        Hellanzb.scroller.segments.remove(self.currentSegment)

        self.currentSegment.articleData = articleData
        self.deferSegmentDecode(self.currentSegment)

        self.currentSegment = None
        self.downloadStartTime = None
        self.readBytes = 0

        reactor.callLater(0, self.fetchNextNZBSegment)
        
    def deferSegmentDecode(self, segment):
        """ Decode the specified segment in a separate thread """
        reactor.callInThread(decode, segment)

    def gotGroup(self, group):
        """ """
        group = group[len(group) - 1]
        self.activeGroups.append(group)
        debug(self.getName() + ' got GROUP: ' + group)

        reactor.callLater(0, self.fetchNextNZBSegment)

    def _stateBody(self, line):
        """ The normal _stateBody converts the list of lines downloaded to a string, we want to
        keep these lines in a list throughout life of the processing (should be more
        efficient) """
        if line != '.':
            self._newLine(line, 0)
        else:
            #self.gotBody('\n'.join(self._endState()))
            self.gotBody(self._endState())

    def _stateHelp(self, line):
        if line != '.':
            self._newLine(line, 0)
        else:
            self.gotHelp('\n'.join(self._endState()))

    def fetchHelp(self):
        debug(self.getName() + ' fetching HELP')
        self.sendLine('HELP')
        self.myState = 'HELP'
        self._newState(self._stateHelp, self.getHelpFailed)

    def gotHelp(self, idle):
        self.myState = None
        debug(self.getName() + ' got HELP')

    def getHelpFailed(self, err):
        "Override for getHelpFailed"
        self.myState = None
        debug(self.getName() + ' got HELP failed: ' + str(err))

    def lineReceived2(self, line):
        # We got data -- reset the anti idle timeout
        
        # Update stats for current segment if we're issuing a BODY command
        #if self.myState == 'BODY':
        #    now = time.time()
        #    self.updateByteCount(len(line))
        #    self.updateStats(now)
            
        #elif self.myState == 'HELP':
        #    # UsenetClient.lineReceived thinks the appropriate HELP response code (100) is
        #    # an error. circumvent it
        #    self._state[0](line)
        #    return
        
        #now = time.time()
        #self.updateByteCount(len(line))
        #self.updateStats(now)
            
        NNTPClient.lineReceived(self, line)

    def lineLengthExceeded(self, line):
        Hellanzb.scroller.prefixScroll('Error!!: LineReceiver.MAX_LENGTH exceeded. size: ' + str(len(line)))
        Hellanzb.scroller.updateLog(True)
        debug('EXCEEDED line length, len: ' + str(len(line)) + ' line: ' + line)

    def updateByteCount(self, lineLen):
        self.readBytes += lineLen
        self.factory.totalReadBytes += lineLen
        self.factory.sessionReadBytes += lineLen
        if self.currentSegment != None:
            self.currentSegment.nzbFile.totalReadBytes += lineLen

    def updateStats(self, now):
        if self.currentSegment == None:
            return

        oldPercentage = self.currentSegment.nzbFile.downloadPercentage
        self.currentSegment.nzbFile.downloadPercentage = min(100,
                                                             int(float(self.currentSegment.nzbFile.totalReadBytes + \
                                                                       self.currentSegment.nzbFile.totalSkippedBytes) /
                                                                 max(1, self.currentSegment.nzbFile.totalBytes) * 100))

        if self.currentSegment.nzbFile.downloadPercentage > oldPercentage:
            elapsed = max(0.1, now - self.currentSegment.nzbFile.downloadStartTime)
            elapsedSession = max(0.1, now - self.factory.sessionStartTime)

            self.currentSegment.nzbFile.speed = self.currentSegment.nzbFile.totalReadBytes / elapsed / 1024.0
            self.factory.sessionSpeed = self.factory.sessionReadBytes / elapsedSession / 1024.0
            
            Hellanzb.scroller.updateLog()
            #if self.currentSegment.nzbFile.downloadPercentage > oldPercentage + 1:
                #reactor.callLater(0, Hellanzb.scroller.updateLog)
            #    Hellanzb.scroller.updateLog()

    def antiIdleConnection(self):
        self.fetchHelp()

    def dataReceived(self, data):
        """ *From Twisted-2.0*
        Supposed to be at least 3x as fast.
        
        Protocol.dataReceived.
        Translates bytes into lines, and calls lineReceived (or
        rawDataReceived, depending on mode.)
        """
        self.resetTimeout()
        self.updateByteCount(len(data))
        self.updateStats(time.time())

        self.__buffer = self.__buffer+data
        lastoffset=0
        while self.line_mode and not self.paused:
            offset=self.__buffer.find(self.delimiter, lastoffset)
            if offset == -1:
                self.__buffer=self.__buffer[lastoffset:]
                if len(self.__buffer) > self.MAX_LENGTH:
                    line=self.__buffer
                    self.__buffer=''
                    return self.lineLengthExceeded(line)
                break
            
            line=self.__buffer[lastoffset:offset]
            lastoffset=offset+len(self.delimiter)
            
            if len(line) > self.MAX_LENGTH:
                line=self.__buffer[lastoffset:]
                self.__buffer=''
                return self.lineLengthExceeded(line)
            why = self.lineReceived(line)
            if why or self.transport and self.transport.disconnecting:
                self.__buffer = self.__buffer[lastoffset:]
                return why
        else:
            if self.paused:
                self.__buffer=self.__buffer[lastoffset:]
            else:
                data=self.__buffer[lastoffset:]
                self.__buffer=''
                if data:
                    return self.rawDataReceived(data)

    #def __str__(self):
    #    return 'str' + self.getName()
    #def __repr__(self):
    #    return 'repr' + self.getName()
    

class ASCIICodes:
    def __init__(self):
        # f/b_ = fore/background
        # d/l  = dark/light
        self.map = {
            'ESCAPE': '\033',
            'F_DBLUE': '34',
            'RESET': '0',
            'KILL_LINE': 'K'
            }
        
    def __getattr__(self, name):
        val = self.map[name]
        if name != 'ESCAPE':
            val = self.map['ESCAPE'] + '[' + val
            if name != 'KILL_LINE':
                val += 'm'
        return val
ACODE = ASCIICodes()
        
class NZBLeecherStatLog:
    """ A basic logger for NZBLeecher. It's uh, not what I really want. I'd rather put more
    time into writing a curses interface. Code submissions greatly appreciated. -pjenvey
    """
    def __init__(self):
        self.size = 0
        self.segments = []
        self.currentLog = None

        self.maxCount = 0 # FIXME: var name

        # Only bother doing the whole UI update after running updateStats this many times
        #self.delay = 2100
        self.delay = 3
        self.wait = 0

        self.connectionPrefix = ACODE.F_DBLUE + '[' + ACODE.RESET + '%s' + ACODE.F_DBLUE + ']' + ACODE.RESET

        self.prefixScrolls = []

    def prefixScroll(self, message):
        self.prefixScrolls.append(message)
        
    def updateLog(self, logNow = False):
        """ Log ticker """
        # Delay the actual log work -- so we don't over-log (too much CPU work in the
        # async loop)
        if not logNow:
            self.wait += 1
            if self.wait < self.delay:
                return
            else:
                self.wait = 0

        currentLog = self.currentLog
        if self.currentLog != None:
            # Kill previous lines,
            #self.currentLog = '\r\033[' + str(self.size) + 'A'
            self.currentLog = '\r\033[' + str(self.maxCount) + 'A'
        else:
            # unless we have just began logging. and in that case, explicitly log the
            # first message
            self.currentLog = ''
            logNow = True

        # Log information we want to prefix the scroll (so it stays on the screen)
        # FIXME: these messages aren't going out to any log file
        # FIXME: ? i dont have to cache these, i could just logNow
        if len(self.prefixScrolls) > 0:
            prefixScroll = ''
            for message in self.prefixScrolls:
                prefixScroll += message + ACODE.KILL_LINE + '\n'
                
            self.currentLog += prefixScroll

        # HACKY:
        # sort by filename, then we'll hide KB/s/percentage for subsequent segments with
        # the same nzbFile as the previous segment
        sortedSegments = self.segments[:]
        sortedSegments.sort(lambda x, y : cmp(x.nzbFile.showFilename, y.nzbFile.showFilename))
        
        lastSegment = None
        i = 0
        for segment in sortedSegments:
            i += 1
            if self.maxCount > 9:
                prettyId = str(i).zfill(2)
            else:
                prettyId = str(i)
            
            # Determine when we've just found the real file name, then use that as the
            # show name
            if segment.nzbFile.showFilenameIsTemp == True and segment.nzbFile.filename != None:
                segment.nzbFile.showFilename = segment.nzbFile.filename
                segment.nzbFile.showFilenameIsTemp = False
                
            if lastSegment != None and lastSegment.nzbFile == segment.nzbFile:
                line = self.connectionPrefix + ' %s' + ACODE.KILL_LINE
                # 58 line width -- approximately 80 - 4 (prefix) - 18 (max suffix)
                self.currentLog += line % (prettyId,
                                           rtruncate(segment.nzbFile.showFilename, length = 58))
            else:
                line = self.connectionPrefix + ' %s - %2d%% @ %.1fKB/s' + ACODE.KILL_LINE
                self.currentLog += line % (prettyId,
                                           rtruncate(segment.nzbFile.showFilename, length = 58),
                                           segment.nzbFile.downloadPercentage, segment.nzbFile.speed)
                
            self.currentLog += '\n\r'

            lastSegment = segment
                
        for fill in range(i + 1, self.maxCount + 1):
            if self.maxCount > 9:
                prettyId = str(fill).zfill(2)
            else:
                prettyId = str(fill)
            self.currentLog += (self.connectionPrefix + ACODE.KILL_LINE) % (prettyId)
            self.currentLog += '\n\r'

        # FIXME: FIXME HA-HA-HACK FIXME
        totalSpeed = 0
        for nsf in Hellanzb.nsfs:
            totalSpeed += nsf.sessionSpeed

        line = self.connectionPrefix + ' %.1fKB/s, %d MB queued ' + ACODE.KILL_LINE
        #self.currentLog += line % ('Total', Hellanzb.totalSpeed,
        self.currentLog += line % ('Total', totalSpeed,
                                   Hellanzb.queue.totalQueuedBytes / 1024 / 1024)

        if logNow or self.currentLog != currentLog:
            scroll(self.currentLog)
            self.prefixScrolls = []

"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
 *                    Ben Bangert <bbangert@groovie.org>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. The name of the author or contributors may not be used to endorse or
 *    promote products derived from this software without specific prior
 *    written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 *
 * $Id$
 */
"""
