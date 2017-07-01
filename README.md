# Google Drive Trash Cleaner
Permanently delete files in Google Drive after X days in trash/bin.

Google Drive doesn't offer the function to delete files after X days in trash/bin. This Python script helps you do this.

## Dependencies
To use the Python script directly
* Python 3 (tested with Python 3.5+)
* package *google-api-python-client*  
run `pip install --upgrade google-api-python-client` to install

To use the Windows binary (download on the [releases](https://github.com/cfbao/google-drive-trash-cleaner/releases) page)
* Windows update [KB2999226](https://support.microsoft.com/en-gb/help/2999226/update-for-universal-c-runtime-in-windows "Update for Universal C Runtime in Windows")

## How-to
Download [cleaner.py](./cleaner.py) (or cleaner.exe) and place it in an empty local folder.

The first time you run it, you will be prompted with Google authorization page asking you to grant permission. Once authorized, an authorization token will be saved in `.credentials\google-drive-trash-cleaner.json` under your home directory (`%UserProfile%` on Windows). You don't need to manually authorize again until you delete this file or revoke permission on your Google account page.

By default, the script tries to retrieve the list of files trashed more than 30 days ago. This may take some time on the first run, because it searches your Google Drive activity history from the very beginning. The script prints the retrieved list of files on screen, and ask you to confirm whether you want to delete them. Once confirmed, these files are permanently deleted from Google Drive, and a new file named `page_token` is saved in the local folder cleaner.py<span>/</span>cleaner.exe is in. 

`page_token` contains a single number that indicates the appropriate starting position in your Google Drive activity history for future searches. Therefore, future runs of the script will be much faster, and `page_token` will be successively updated.

There are a few command line options that allows for some customizations. Run `cleaner -h` to learn about them.
