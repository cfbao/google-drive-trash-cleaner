# Copyright (C) 2017 - Chenfeng Bao
#
# This program is free software; you can redistribute it and/or modify it 
# under the terms of the GNU General Public License; either version 3 of 
# the License, or (at your option) any later version.
# You should have received a copy of the GNU General Public License 
# along with this program; if not, see <http://www.gnu.org/licenses>.

import httplib2
import os
import sys
import io
import builtins
import argparse
import time
import calendar
import logging
import warnings

from apiclient import discovery
from apiclient.errors import HttpError
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

if getattr(sys, 'frozen', False):
    # running in a bundle
    CLEANER_PATH = sys.executable
else:
    # running as a normal Python script
    CLEANER_PATH = os.path.realpath(__file__)
PAGE_TOKEN_FILE = os.path.join(os.path.dirname(CLEANER_PATH), 'page_token')
CREDENTIAL_FILE = os.path.join(os.path.expanduser('~'), '.credentials', 'google-drive-trash-cleaner.json')

CLIENT_CREDENTIAL = {
    "client_id" : "359188752904-817oqa6dr7elufur5no09q585trpqf1l.apps.googleusercontent.com",
    "client_secret" : "uZtsDf5vaUm8K-kZLZETmsYi",
    "scope" : 'https://www.googleapis.com/auth/drive',
    "redirect_uri" : "urn:ietf:wg:oauth:2.0:oob",
    "token_uri" : "https://accounts.google.com/o/oauth2/token",
    "auth_uri" : "https://accounts.google.com/o/oauth2/auth",
    "revoke_uri" : "https://accounts.google.com/o/oauth2/revoke",
    "pkce" : True
}

PAGE_SIZE_LARGE = 1000
PAGE_SIZE_SMALL = 100
PAGE_SIZE_SWITCH_THRESHOLD = 3000
RETRY_NUM = 3
RETRY_INTERVAL = 2
TIMEOUT_DEFAULT = 300

class TimeoutError(Exception):
    pass

class PageTokenFile:
    def __init__(self, filePath):
        self.path = filePath
    
    def get(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                pageToken = int(f.read())
        except (FileNotFoundError, ValueError):
            pageToken = 0
        return pageToken
    
    def save(self, pageToken):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write(str(pageToken))

class SafePrinter:
    class _SafeTextWrapper:
        def __init__(self, unsafeTextFile, error):
            if not isinstance(unsafeTextFile, io.TextIOBase):
                raise TypeError()
            self.unsafeTextFile = unsafeTextFile
            self.encoding = unsafeTextFile.encoding
            self.error = error
        def write(self, text):
            self.unsafeTextFile.write(text.encode(self.encoding, self.error).decode(self.encoding, 'ignore'))
        def flush(self):
            self.unsafeTextFile.flush()
    
    def __init__(self, defaultFile=None, error='backslashreplace'):
        if error not in ['replace', 'xmlcharrefreplace', 'backslashreplace', 'namereplace']:
            raise ValueError("`error` must be one of 'replace', 'xmlcharrefreplace', 'backslashreplace', 'namereplace'")
        self.defaultFile = defaultFile or sys.stdout
        self.error = error
        self.wrappers = {id(self.defaultFile): SafePrinter._SafeTextWrapper(self.defaultFile, self.error)}
    
    def get_print(self):
        def print(*args, **kwargs):
            file = kwargs.get('file') or self.defaultFile
            if id(file) not in self.wrappers:
                self.wrappers[id(file)] = SafePrinter._SafeTextWrapper(file, self.error)
            kwargs['file'] = self.wrappers[id(file)]
            builtins.print(*args, **kwargs)
        return print
    
    def clear(self):
        delList = []
        for id in self.wrappers:
            if self.wrappers[id].unsafeTextFile.closed:
                delList.append(id)
        for id in delList:
            del self.wrappers[id]
    
    def purge(self):
        self.wrappers.clear()
        self.wrappers = {id(self.defaultFile): SafePrinter._SafeTextWrapper(self.defaultFile, self.error)}

try:
    print = SafePrinter().get_print()
except TypeError:
    sys.stderr.write('`SafePrinter` failed to initialize. Please contact the developer.\n')
    sys.exit(-1)

def main():
    flags = parse_cmdline()
    logger = configure_logs(flags.logfile)
    pageTokenFile = PageTokenFile(flags.ptokenfile)
    for i in range(RETRY_NUM):
        try:
            service = build_service(flags)
            pageToken = pageTokenFile.get()
            deletionList, pageTokenBefore, pageTokenAfter = \
                get_deletion_list(service, pageToken, flags)
            pageTokenFile.save(pageTokenBefore)
            listEmpty = delete_old_files(service, deletionList, flags)
        except client.HttpAccessTokenRefreshError:
            print('Authentication error')
        except httplib2.ServerNotFoundError as e:
            print('Error:', e)
        except TimeoutError:
            print('Timeout: Google backend error.')
            print('Retries unsuccessful. Abort action.')
            return
        else:
            break
        time.sleep(RETRY_INTERVAL)
    else:
        print("Retries unsuccessful. Abort action.")
        return
    
    if listEmpty:
        pageTokenFile.save(pageTokenAfter)

def parse_cmdline():
    parser = argparse.ArgumentParser()
    # flags required by oauth2client.tools.run_flow(), hidden
    parser.add_argument('--auth_host_name', action='store', default='localhost', help=argparse.SUPPRESS)
    parser.add_argument('--noauth_local_webserver', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--auth_host_port', action='store', nargs='*', default=[8080, 8090], type=int, help=argparse.SUPPRESS)
    parser.add_argument('--logging_level', action='store', default='ERROR', 
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], help=argparse.SUPPRESS)
    # flags defined by cleaner.py
    parser.add_argument('-a', '--auto', action='store_true', 
            help='Automatically delete older trashed files in Google Drive '
                 'without prompting user for confirmation')
    parser.add_argument('-v', '--view', action='store_true', 
            help='Only view which files are to be deleted without deleting them')
    parser.add_argument('-d', '--days', action='store', type=int, default=30, metavar='#',
            help='Number of days files can remain in Google Drive trash '
                 'before being deleted. Default is %(default)s')
    parser.add_argument('-q', '--quiet', action='store_true', 
            help='Quiet mode. Only show file count.')
    parser.add_argument('-t', '--timeout', action='store', type=int, default=TIMEOUT_DEFAULT, metavar='SECS',
            help='Specify timeout period in seconds. Default is %(default)s')
    parser.add_argument('-m', '--mydriveonly', action='store_true',
            help="Only delete files in the 'My Drive' hierarchy, excluding those in 'Computers' etc.")
    parser.add_argument('--noprogress', action='store_true',
            help="Don't show scanning progress. Useful when directing output to files.")
    parser.add_argument('--fullpath', action='store_true',
            help="Show full path to files. May be slow for a large number of files. "
                "NOTE: the path shown is the 'current' path, "
                "may be different from the original path (when trashing) "
                "if the original parent folder has moved.")
    parser.add_argument('--logfile', action='store', metavar='PATH',
            help='Path to log file. Default is no logs')
    parser.add_argument('--ptokenfile', action='store', default=PAGE_TOKEN_FILE, metavar='PATH',
            help="Path to page token file. Default is \"{}\" in %(prog)s's parent folder".
                    format(os.path.basename(PAGE_TOKEN_FILE)))
    parser.add_argument('--credfile', action='store', default=CREDENTIAL_FILE, metavar='PATH',
            help="Path to OAuth2Credentials file. Default is %(default)s")
    flags = parser.parse_args()
    if flags.days < 0:
        parser.error('argument --days must be nonnegative')
    if flags.timeout < 0:
        parser.error('argument --timeout must be nonnegative')
    if flags.logfile and flags.logfile.strip():
        flags.logfile = os.path.realpath(flags.logfile)
        os.makedirs(os.path.dirname(flags.logfile),    exist_ok=True)
    if flags.quiet and not flags.logfile:
        flags.fullpath = False
    flags.ptokenfile = os.path.realpath(flags.ptokenfile)
    flags.credfile   = os.path.realpath(flags.credfile)
    os.makedirs(os.path.dirname(flags.ptokenfile), exist_ok=True)
    os.makedirs(os.path.dirname(flags.credfile),   exist_ok=True)
    return flags

def configure_logs(logPath):
    logger = logging.getLogger('gdtc')
    logger.setLevel(logging.INFO)
    if not logPath:
        return logger
    logPath = logPath.strip('"')
    open(logPath, 'a').close()
    fileHandler = logging.FileHandler(
        logPath, mode='a', encoding='utf-8')
    logger.addHandler(fileHandler)
    return logger

def build_service(flags):
    credentials = get_credentials(flags)
    http = credentials.authorize(httplib2.Http())
    service = discovery.build('drive', 'v3', http=http)
    return service

def get_credentials(flags):
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    store = Storage(flags.credfile)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        credentials = store.get()
    if not credentials or credentials.invalid:
        flow = client.OAuth2WebServerFlow(**CLIENT_CREDENTIAL)
        credentials = tools.run_flow(flow, store, flags)
        print('credential file saved at\n\t' + flags.credfile)
    return credentials

def get_deletion_list(service, pageToken, flags, pathFinder=None):
    """Get list of files to be deleted and page token for future use.
    
    deletionList, pageTokenBefore, pageTokenAfter
        = get_deletion_list(service, pageToken, maxTrashDays, timeout)
    
    Iterate through Google Drive change list to find trashed files in order 
    of trash time. Return a list of files trashed more than maxTrashDays 
    seconds ago and a new page token for future use.
    
    service:        Google API service object
    pageToken:      An integer referencing a position in Drive change list.
                    Only changes made after this point will be checked. By 
                    assumption, trashed files before this point are all 
                    deleted.
    deletionList:   List of trashed files to be deleted, in ascending order of 
                    trash time. Each file is represented as a dictionary with 
                    keys {'fileId', 'time', 'name'}.
    flags:          Flags parsed from command line. Should contain the 
                    following attributes:
                    --noprogress    don't show scanning progress
                    --fullpath      show full path
                    --mydriveonly   restrict to my drive
                    --quiet         don't show individual file info
                    --timeout       timeout in seconds
                    --days          maximum days in trash
    pageTokenBefore:
                    An integer representing a point in Drive change list, 
                    >= 'pageToken'.
                    This page token is before everything in deletionList. Can 
                    be used as future pageToken no matter what.
    pageTokenAfter: An integer representing a point in Drive change list, 
                    >= 'pageToken'.
                    Can be used as future pageToken only if everything in 
                    deletionList is deleted.
    """
    response = execute_request(service.changes().getStartPageToken(), flags.timeout)
    latestPageToken = int(response.get('startPageToken'))
    currentTime = time.time()
    deletionList = []
    if not pageToken:
        pageToken = 1
    pageTokenBefore = pageToken
    pageSize = PAGE_SIZE_LARGE
    progress = ScanProgress(quiet=flags.quiet, noProgress=flags.noprogress)
    if not pathFinder and flags.fullpath:
        pathFinder = PathFinder(service)
    while pageToken:
        if latestPageToken - int(pageToken) < PAGE_SIZE_SWITCH_THRESHOLD:
            pageSize = PAGE_SIZE_SMALL
        request = service.changes().list(
                    pageToken=pageToken, includeRemoved=False,
                    pageSize=pageSize, restrictToMyDrive=flags.mydriveonly,
                    fields='nextPageToken,newStartPageToken,'
                    'changes(fileId,time,file(name,parents,explicitlyTrashed,ownedByMe))'
                    )
        response = execute_request(request, flags.timeout)
        items = response.get('changes', [])
        for item in items:
            itemTime = parse_time(item['time'])
            if currentTime - itemTime < flags.days*24*3600:
                progress.clear_line()
                return deletionList, pageTokenBefore, pageToken
            progress.print_time(item['time'])
            if item['file']['explicitlyTrashed'] and item['file']['ownedByMe']:
                if flags.fullpath:
                    disp = pathFinder.get_path(item['fileId'], fileRes=item['file'])
                else:
                    disp = item['file']['name']
                progress.found(item['time'], disp)
                deletionList.append({'fileId': item['fileId'], 'time': item['time'],
                                        'name': disp})
        pageToken = response.get('nextPageToken')
        if not deletionList:
            pageTokenBefore = pageToken
    progress.clear_line()
    return deletionList, pageTokenBefore, int(response.get('newStartPageToken'))

def delete_old_files(service, deletionList, flags):
    """Print and delete files in deletionList
    
    listEmpty = delete_old_files(service, deletionList, flags)
    
    service:        Google API service object
    deletionList:   List of trashed files to be deleted, in ascending order of 
                    trash time. Each file is represented as a dictionary with 
                    keys {'fileId', 'time', 'name'}.
    flags:          Flags parsed from command line arguments. In 
                    particular, automatic deletion (no user prompt) and view-
                    only mode (print but don't delete) are supported.
    listEmpty:      Return True if deletionList is either empty on input or 
                    emptied by this function, False otherwise.
    """
    logger = logging.getLogger('gdtc')
    n = len(deletionList)
    if n == 0:
        print('No files to be deleted')
        return True
    if flags.view:
        if n == 1:
            print('{:} file/folder trashed more than {:} days ago'.format(n, flags.days))
        else:
            print('{:} file/folder(s) trashed more than {:} days ago'.format(n, flags.days))
        return False
    if not flags.auto:
        confirmed = ask_usr_confirmation(n)
        if not confirmed:
            return False
    print('Deleting...')
    for item in reversed(deletionList):
        request = service.files().delete(fileId = item['fileId'])
        execute_request(request, flags.timeout)
        logger.info(item['time'] + ''.ljust(4) + item['name'])
    print('Files successfully deleted')
    return True

class ScanProgress:
    def __init__(self, quiet, noProgress):
        self.printed = "0000-00-00"
        self.noItemYet = True
        self.quiet = quiet
        self.noProgress = noProgress
    
    def print_time(self, timeStr):
        """print yyyy-mm-dd only if not yet printed"""
        if self.noProgress:
            return
        ymd = timeStr[:10]
        if ymd > self.printed:
            print('\rScanning files trashed on ' + ymd, end='')
            self.printed = ymd
    
    def found(self, time, name):
        """found an item, print its info"""
        if self.quiet:
            return
        if not self.noProgress:
            print('\r' + ''.ljust(40) + '\r', end='')
        if self.noItemYet:
            print('Date trashed'.ljust(24) + ''.ljust(4) + 'File Name/Path')
            self.noItemYet = False
        print(time + ''.ljust(4) + name)
        if not self.noProgress:
            print('\rScanning files trashed on ' + self.printed, end='')
    
    def clear_line(self):
        print('\r' + ''.ljust(40) + '\r', end='')
        print()

class PathFinder:
    def __init__(self, service, cache=None):
        self.service = service
    # each item in self.cache is a list with 2 elements
    # self.cache[id][0] is the full path of id
    # self.cache[id][1] is the number of times id has been queried
        if cache:
            self.cache = cache
        else:
            self.cache = dict()
    # self.expanded contains all ids that have all their children cached
        self.expanded = set()
    
    def get_path(self, id, fileRes=None):
        """Find the full path for id
        
        fileRes:    File resource for id. 
                    Must have 'name' and 'parents' attributes if available.
                    If None or unspecified, an API call is made to query"""
        if id in self.cache:
            if self.cache[id][1]>1 and id not in self.expanded:
                # find and cache all children if id is requested more than once
                self.expand_cache(id)
            self.cache[id][1] += 1
            return self.cache[id][0]
        if not fileRes:
            request = self.service.files().get(fileId=id, fields='name,parents')
            fileRes = execute_request(request)
        try:
            parentId = fileRes['parents'][0]
            self.cache[id] = [self.get_path(parentId) + os.sep + fileRes['name'], 1]
        except KeyError:
            self.cache[id] = [fileRes['name'], 1]
        return self.cache[id][0]
    
    def expand_cache(self, id):
        if id in self.expanded:
            return
        npt = None
        while True:
            request = self.service.files().list(
                    q="'{:}' in parents and trashed=true".format(id), 
                    pageToken=npt, 
                    fields="files(id,name),nextPageToken",
                    pageSize=1000)
            response = execute_request(request)
            for file in response['files']:
                if file['id'] in self.cache:
                    continue
                self.cache[file['id']] = [self.cache[id][0] + os.sep + file['name'], 0]
            try:
                npt = response['nextPageToken']
            except KeyError:
                break
        self.expanded.add(id)
    
    def clear():
        self.cache.clear()

def execute_request(request, timeout=TIMEOUT_DEFAULT):
    """Execute Google API request
    Automatic retry upon Google backend error (500) until timeout
    """
    while timeout >= 0:
        try:
            response = request.execute()
        except HttpError as e:
            if int(e.args[0]['status']) == 500:
                timeout -= RETRY_INTERVAL
                time.sleep(RETRY_INTERVAL)
                continue
            raise e
        else:
            return response
    raise TimeoutError

def ask_usr_confirmation(n):
    while True:
        if n == 1:
            usrInput = input('Confirm deleting this file/folder? (Y/N)\n')
        else:
            usrInput = input('Confirm deleting these {:} files/folders? (Y/N)\n'.format(n))
        if usrInput.strip().lower() == 'y':
            return True
        elif usrInput.strip().lower() == 'n':
            return False

def parse_time(rfc3339):
    """parse the RfC 3339 time given by Google into Unix time"""
    time_str = rfc3339.split('.')[0]
    return calendar.timegm(time.strptime(time_str, '%Y-%m-%dT%H:%M:%S'))

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped by user')
