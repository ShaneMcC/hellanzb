"""

ArticleDecoder - Decode and assemble files from usenet articles (nzbSegments)

(c) Copyright 2005 Philip Jenvey
[See end of file]
"""
import binascii, os, re, shutil, string
from twisted.internet import reactor
from zlib import crc32
from Hellanzb.Daemon import handleNZBDone
from Hellanzb.Log import *

__id__ = '$Id$'

# Decode types enum
UNKNOWN, YENCODE, UUENCODE = range(3)

# FIXME: FatalErrors shouldn't really be thrown. They should print the problem and
# continue on. These FatalErrors could make the segment it died on lack a file on the
# filesystem -- this is bad. hellanzb won't assemble the final File unless all the
# segments are on the filesystem. The decode() function could catch them and act
# appropriately

def decode(segment):
    """ Decode the NZBSegment's articleData to it's destination. Toggle the NZBSegment
    instance as having been decoded, then assemble all the segments together if all their
    decoded segment filenames exist """
    # FIXME: should need to try/ this call?
    decodeArticleData(segment)

    # FIXME: maybe call everything below this postProcess. have postProcess called when --
    # during the queue instantiation?
    if segment.nzbFile.isAllSegmentsDecoded():
        assembleNZBFile(segment.nzbFile)
    debug('Decoded segment: ' + segment.getDestination())

def stripArticleData(articleData):
    """ Rip off leading/trailing whitespace from the articleData list """
    try:
        # Only rip off the first leading whitespace
        #while articleData[0] == '':
        if articleData[0] == '':
            articleData.pop(0)

        # Trailing
        while articleData[-1] == '':
            articleData.pop(-1)
    except IndexError:
        pass

def parseArticleData(segment, justExtractFilename = False):
    """ get the article's filename from the articleData. if justExtractFilename == False,
    continue parsing the articleData -- decode that articleData (uudecode/ydecode) to the
    segment's destination """
    # FIXME: rename if fileDestination exists? what to do w /existing files?

    if segment.articleData == None:
        raise FatalError('Could not getFilenameFromArticleData')

    # First, clean it
    stripArticleData(segment.articleData)

    cleanData = []
    encodingType = UNKNOWN
    withinData = False
    index = -1
    for line in segment.articleData:
        index += 1
        #info('index: ' + str(index) + ' line: ' + line)

        if withinData:
            # un-double-dot any lines :\
            if line[:2] == '..':
                line = line[1:]
                segment.articleData[index] = line

        # After stripping the articleData, we should find a yencode header, uuencode
        # header, or a uuencode part header (an empty line)
        if line.startswith('=ybegin'):
            # See if we can parse the =ybegin line
            ybegin = ySplit(line)
            
            if not ('line' in ybegin and 'size' in ybegin and 'name' in ybegin):
                # FIXME: show filename information
                raise FatalError('* Invalid =ybegin line in part %d!' % segment.number)

            setRealFileName(segment, ybegin['name'])
            encodingType = YENCODE

        elif line.startswith('=ypart'):
            # FIXME: does ybegin always ensure a ypart on the next line?
            withinData = True
            
        elif line.startswith('=yend'):
            yend = ySplit(line)
            if 'pcrc32' in yend:
                segment.crc = '0' * (8 - len(yend['pcrc32'])) + yend['pcrc32'].upper()
            elif 'crc32' in yend and yend.get('part', '1') == '1':
                segment.crc = '0' * (8 - len(yend['crc32'])) + yend['crc32'].upper()

        elif line.startswith('begin '):
            filename = line.rstrip().split(' ', 2)[2]
            if not filename:
                # FIXME: show filename information
                raise FatalError('* Invalid begin line in part %d!' % segment.number)
            setRealFileName(segment, filename)
            encodingType = UUENCODE
            withinData = True
        elif line == '':
            continue
        elif not withinData and encodingType == YENCODE:
            # Found ybegin, but no ypart. withinData should have started on the previous
            # line -- so instead we have to process the current line
            withinData = True

            # un-double-dot any lines :\
            if line[:2] == '..':
                line = line[1:]
                segment.articleData[index] = line
        elif not withinData:
            # Assume this is a subsequent uuencode segment
            withinData = True
            encodingType = UUENCODE

    # FIXME: could put this check even higher up
    if justExtractFilename:
        return

    decodeSegmentToFile(segment, encodingType)
    del cleanData
    del segment.articleData
    segment.articleData = '' # We often check it for == None
decodeArticleData=parseArticleData

def setRealFileName(segment, filename):
    """ Set the actual filename of the segment's parent nzbFile. If the filename wasn't
    already previously set, set the actual filename atomically and also atomically rename
    known temporary files belonging to that nzbFile to use the new real filename """
    noFileName = segment.nzbFile.filename == None
    if noFileName and segment.number == 1:
        # We might have been using a tempFileName previously, and just succesfully found
        # the real filename in the articleData. Immediately rename any files that were
        # using the temp name
        segment.nzbFile.tempFileNameLock.acquire()
        segment.nzbFile.filename = filename

        tempFileNames = {}
        for nzbSegment in segment.nzbFile.nzbSegments:
            tempFileNames[nzbSegment.getTempFileName()] = os.path.basename(nzbSegment.getDestination())

        from Hellanzb import WORKING_DIR
        for file in os.listdir(WORKING_DIR):
            if file in tempFileNames:
                newDest = tempFileNames.get(file)
                shutil.move(WORKING_DIR + os.sep + file,
                            WORKING_DIR + os.sep + newDest)

        segment.nzbFile.tempFileNameLock.release()
    else:
        segment.nzbFile.filename = filename

def decodeSegmentToFile(segment, encodingType = YENCODE):
    """ Decode the clean data (clean as in it's headers (mime and yenc/uudecode) have been
    removed) list to the specified destination """
    decodedLines = []
    if encodingType == YENCODE:
        decodedLines = yDecode(segment.articleData)

        # CRC check
        if segment.crc == None:
            reactor.callFromThread(Hellanzb.scroller.prefixScroll, segment.nzbFile.showFilename + \
                                   ' segment: ' + str(segment.number) + \
                                   ' does not have a valid CRC/yend line!')
            # FIXME: I've seen CRC errors at the end of archive cause logNow = True to
            # print I think after handleNZBDone appends a newline (looks like crap)
            reactor.callFromThread(Hellanzb.scroller.updateLog, True)
        else:
            decoded = ''.join(decodedLines)
            crc = '%08X' % (crc32(decoded) & 2**32L - 1)
            if crc != segment.crc:
                message = segment.nzbFile.showFilename + ' segment ' + str(segment.number) + \
                    ': CRC mismatch ' + crc + ' != ' + segment.crc
                reactor.callFromThread(Hellanzb.scroller.prefixScroll, message)
                reactor.callFromThread(Hellanzb.scroller.updateLog, True)
                # FIXME: this needs to go out to the normal log file
                debug(message)
            del decoded
            
        out = open(segment.getDestination(), 'wb')
        for line in decodedLines:
            out.write(line)
        out.close()

        # Get rid of all this data now that we're done with it
        debug('YDecoded articleData to file: ' + segment.getDestination())

    elif encodingType == UUENCODE:
        try:
            decodedLines = UUDecode(segment.articleData)
        except binascii.Error, msg:
            error('\n* Decode failed in file: %s (part number: %d) error: %s' % \
                  (segment.getDestination(), segment.number, msg))
            debug('\n* Decode failed in file: %s (part number: %d) error: %s' % \
                  (segment.getDestination(), segment.number, msg))

        out = open(segment.getDestination(), 'wb')
        for line in decodedLines:
            out.write(line)
        out.close()

        # Get rid of all this data now that we're done with it
        debug('UUDecoded articleData to file: ' + segment.getDestination())
        
    else:
        debug('FIXME: Did not YY/UDecode!!')
        #raise FatalError('doh!')

# From effbot.org/zone/yenc-decoder.htm -- does not suffer from yDecodeOLD's bug -pjenvey
yenc42 = string.join(map(lambda x: chr((x-42) & 255), range(256)), '')
yenc64 = string.join(map(lambda x: chr((x-64) & 255), range(256)), '')
def yDecode(dataList):
    buffer = []
    index = -1
    for line in dataList:
        index += 1
        if index <= 5 and (line[:7] == '=ybegin' or line[:6] == '=ypart'):
            continue
        elif not line or line[:5] == '=yend':
            break

        if line[-2:] == '\r\n':
            line = line[:-2]
        elif line[-1:] in '\r\n':
            line = line[:-1]

        data = string.split(line, '=')
        buffer.append(string.translate(data[0], yenc42))
        for data in data[1:]:
            data = string.translate(data, yenc42)
            buffer.append(string.translate(data[0], yenc64))
            buffer.append(data[1:])

    return buffer
                                 
YSPLIT_RE = re.compile(r'(\S+)=')
def ySplit(line):
        'Split a =y* line into key/value pairs'
        fields = {}
        
        parts = YSPLIT_RE.split(line)[1:]
        if len(parts) % 2:
                return fields
        
        for i in range(0, len(parts), 2):
                key, value = parts[i], parts[i+1]
                fields[key] = value.strip()
        
        return fields

def UUDecode(dataList):
    buffer = []

    index = -1
    for line in dataList:
        index += 1

        if index <= 5 and (not line or line[:6] == 'begin '):
            continue
        elif not line or line[:3] == 'end':
            break
        
        if line[-2:] == '\r\n':
            line = line[:-2]
        elif line[-1:] in '\r\n':
            line = line[:-1]

        # NOTE: workaround imported from Newsleecher.HeadHoncho, is this necessary?
        try:
            data = binascii.a2b_uu(line)
            buffer.append(data)
        except binascii.Error, msg:
            # Workaround for broken uuencoders by /Fredrik Lundh
            try:
                #warn('UUEncode workaround')
                nbytes = (((ord(line[0])-32) & 63) * 4 + 5) / 3
                data = binascii.a2b_uu(line[:nbytes])
                buffer.append(data)
            except binascii.Error, msg:
                error('\nUUDecode failed, line: ' + repr(line))
                debug('\nUUDecode failed, line: ' + repr(line))
                raise

    return buffer

def assembleNZBFile(nzbFile):
    """ Assemble the final file from all the NZBFile's decoded segments """
    # FIXME: does someone has to pad the file if we have broken pieces?
    
    # FIXME: don't overwrite existing files???
    file = open(nzbFile.getDestination(), 'wb')
    for nzbSegment in nzbFile.nzbSegments:

        decodedSegmentFile = open(nzbSegment.getDestination(), 'rb')
        for line in decodedSegmentFile.readlines():
            if line == '':
                break

            file.write(line)
        decodedSegmentFile.close()
        
        os.remove(nzbSegment.getDestination())

    file.close()
    Hellanzb.queue.fileDone(nzbFile)
    
    debug('Assembled file: ' + nzbFile.getDestination() + ' from segment files: ' + \
          str([ nzbSegment.getDestination() for nzbSegment in nzbFile.nzbSegments ]))

    # After assembling a file, check the contents of the filesystem to determine if we're done 
    tryFinishNZB(nzbFile.nzb)

def tryFinishNZB(nzb):
    """ Determine if the NZB download/decode process is done for the specified NZB -- if it's
    done, trigger handleNZBDone. We'll call this check everytime we finish processing an
    nzbFile """
    start = time.time()
    done = True

    # Only loop through the nzb nzbFile's that lie in the Queue (and belong to the
    # specified NZB)
    Hellanzb.queue.nzbFilesLock.acquire()
    queueFilesCopy = Hellanzb.queue.nzbFiles.copy()
    Hellanzb.queue.nzbFilesLock.release()

    for nzbFile in queueFilesCopy:
        if nzbFile not in nzb.nzbFileElements:
            continue
        
        if nzbFile.needsDownload():
            debug('NOT DONE, file: ' + nzbFile.getDestination())
            done = False
            break

    if done:
        debug('tryFinishNZB: finished donwloading NZB: ' + nzb.archiveName)
        reactor.callFromThread(handleNZBDone, nzb.nzbFileName)
        
    finish = time.time() - start
    debug('tryFinishNZB (' + str(done) + ') took: ' + str(finish) + ' seconds')
    return done
        
"""
/*
 * Copyright (c) 2005 Philip Jenvey <pjenvey@groovie.org>
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
