import httplib2
import os
import sys
import argparse
import time
import calendar
import logging

from apiclient import discovery
from apiclient.errors import HttpError
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage

if getattr(sys, 'frozen', False):
	# running in a bundle
    DIR_PATH = os.path.dirname(sys.executable)
else:
    # running as a normal Python script
    DIR_PATH = os.path.dirname(os.path.realpath(__file__))

PAGE_TOKEN_FILE = os.path.join(DIR_PATH, 'page_token')
CREDENTIAL_DIR = os.path.join(os.path.expanduser('~'), '.credentials')

CLIENT_ID = "359188752904-817oqa6dr7elufur5no09q585trpqf1l.apps.googleusercontent.com"
CLIENT_SECRET = "uZtsDf5vaUm8K-kZLZETmsYi"
SCOPE = 'https://www.googleapis.com/auth/drive'
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
TOKEN_URI = "https://accounts.google.com/o/oauth2/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
REVOKE_URI = "https://accounts.google.com/o/oauth2/revoke"

LOG_NAME = 'dtad.log'
PAGE_SIZE_LARGE = 1000
PAGE_SIZE_SMALL = 100
PAGE_SIZE_SWITCH_THRESHOLD = 3000
RETRY_NUM = 3
RETRY_INTERVAL = 2
TIMEOUT_DEFAULT = 300

class TimeoutError(Exception):
    pass

def main():
    flags = parse_cmdline()
    logger = configure_logs(flags.logDir)
    for i in range(RETRY_NUM):
        try:
            service = build_service(flags)
            pageToken = get_stored_token()
            deletionList, futurePageToken = get_deletion_list(
                            service, pageToken, flags.days, flags.timeout)
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
        store_future_token(futurePageToken)

def parse_cmdline():
    parser = argparse.ArgumentParser(parents=[tools.argparser])
    parser.add_argument('-a', '--auto', action='store_true', 
            help='Automatically deletes older trashed files in Google Drive '
                 'without prompting user for confirmation')
    parser.add_argument('-v', '--view', action='store_true', 
            help='Only view which files are to be deleted without deleting them')
    parser.add_argument('-d', '--days', action='store', type=int, default=30, metavar='D',
            help='Number of days files can remain in Google Drive trash '
                 'before being deleted. Default is 30')
    parser.add_argument('-t', '--timeout', action='store', type=int, default=TIMEOUT_DEFAULT, metavar='T',
            help='Specify timeout period in seconds. Default is {:d}'.format(TIMEOUT_DEFAULT))
    parser.add_argument('-l', '--logDir', action='store', metavar='LOG_PATH',
            help='Directory of log file. Default is no logs')
    flags = parser.parse_args()
    if flags.days < 0:
        parser.error('argument --days must be nonnegative')
    if flags.timeout < 0:
        parser.error('argument --timeout must be nonnegative')
    return flags

def configure_logs(logDir):
    logger = logging.getLogger('dtad')
    logger.setLevel(logging.INFO)
    if not logDir:
        return logger
    logDir = logDir.strip('"')
    logPath = os.path.join(logDir, LOG_NAME)
    open(logPath, 'a').close()
    fileHandler = logging.FileHandler(
        logPath, mode='a', encoding='utf-8')
    logger.addHandler(fileHandler)
    return logger

def build_service(flags=None):
    credentials = get_credentials(flags)
    http = credentials.authorize(httplib2.Http())
    service = discovery.build('drive', 'v3', http=http)
    return service

def get_credentials(flags=None):
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.

    Returns:
        Credentials, the obtained credential.
    """
    os.makedirs(CREDENTIAL_DIR, exist_ok=True)
    credential_path = os.path.join(CREDENTIAL_DIR, 'google-drive-trash-cleaner.json')
    store = Storage(credential_path)
    credentials = store.get()
    if not credentials or credentials.invalid:
        flow = client.OAuth2WebServerFlow(
                client_id = CLIENT_ID,
                client_secret = CLIENT_SECRET,
                scope = SCOPE,
                redirect_uri = REDIRECT_URI,
                token_uri = TOKEN_URI,
                auth_uri = AUTH_URI,
                revoke_uri = REVOKE_URI,
                pkce = True,
                )
        credentials = tools.run_flow(flow, store, flags)
        logging.getLogger('dtad').info('Storing credentials to ' + credential_path)
    return credentials

def get_stored_token():
    try:
        with open(PAGE_TOKEN_FILE, 'r', encoding='utf-8') as f:
            pageToken = int(f.read())
    except (FileNotFoundError, ValueError):
        pageToken = 0
    return pageToken

def store_future_token(futurePageToken):
    with open(PAGE_TOKEN_FILE, 'w', encoding='utf-8') as f:
        f.write(str(futurePageToken))

def get_deletion_list(service, pageToken, maxTrashDays, timeout):
    """Get list of files to be deleted and page token for future use.
    
    deletionList, futurePageToken = get_deletion_list(service, pageToken,
                                                    maxTrashDays, timeout)
    
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
    futurePageToken:
                    An integer representing a point in Drive change list, 
                    >= 'pageToken'. If all files in 'deletionList' are deleted
                    or if 'deletionList' is empty, it can be used as 
                    'pageToken' in a future call of this function; if not, it 
                    should be discarded.
    """
    response = execute_request(service.changes().getStartPageToken(), timeout)
    latestPageToken = int(response.get('startPageToken'))
    currentTime = time.time()
    deletionList = []
    if not pageToken:
        pageToken = 1
    pageSize = PAGE_SIZE_LARGE
    while pageToken:
        if latestPageToken - int(pageToken) < PAGE_SIZE_SWITCH_THRESHOLD:
            pageSize = PAGE_SIZE_SMALL
        request = service.changes().list(
                    pageToken=pageToken, includeRemoved=False,
                    pageSize=pageSize, restrictToMyDrive=True,
                    fields='nextPageToken,newStartPageToken,'
                    'changes(fileId,time,file(name,explicitlyTrashed))'
                    )
        response = execute_request(request, timeout)
        items = response.get('changes', [])
        for item in items:
            itemTime = parse_time(item['time'])
            if currentTime - itemTime < maxTrashDays*24*3600:
                return deletionList, pageToken
            if item['file']['explicitlyTrashed']:
                deletionList.append({'fileId': item['fileId'], 'time': item['time'],
                                        'name': item['file']['name']})
        pageToken = response.get('nextPageToken')
    return deletionList, int(response.get('newStartPageToken'))

def delete_old_files(service, deletionList, flags):
    """Print and delete files in deletionList
    
    listEmpty = delete_old_files(service, deletionList, flags)
    
    service:        Google API service object
    deletionList:   List of trashed files to be deleted, in ascending order of 
                    trash time. Each file is represented as a dictionary with 
                    keys {'fileId', 'time', 'name'}.
    flags:          Flags as interpreted from command line arguments. In 
                    particular, automatic deletion (no user prompt) and view-
                    only mode (print but don't delete) are supported.
    listEmpty:      Return True if deletionList is either empty on input or 
                    emptied by this function, False otherwise.
    """
    logger = logging.getLogger('dtad')
    if not deletionList:
        print('No files to be deleted')
        return True
    
    print('Date trashed'.ljust(24) + ''.ljust(4) + 'Filename')
    for item in deletionList:
        print(item['time'] + ''.ljust(4) + item['name'])
    print('')
    if flags.view:
        return False
    if not flags.auto:
        confirmed = ask_usr_confirmation()
        if not confirmed:
            return False
    print('Deleting...')
    for item in reversed(deletionList):
        request = service.files().delete(fileId = item['fileId'])
        execute_request(request, flags.timeout)
        logger.info(item['time'] + ''.ljust(4) + item['name'])
    print('Files successfully deleted')
    return True

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

def ask_usr_confirmation():
    while True:
        usrInput = input('Confirm deleting these files? (Y/N)\n')
        if usrInput.strip().lower() == 'y':
            return True
        elif usrInput.strip().lower() == 'n':
            return False

def parse_time(rfc3339):
    """parse the RfC 3339 time given by Google into Unix time"""
    time_str = rfc3339.split('.')[0]
    return calendar.timegm(time.strptime(time_str, '%Y-%m-%dT%H:%M:%S'))

if __name__ == '__main__':
    main()