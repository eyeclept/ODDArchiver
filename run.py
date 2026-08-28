"""
File:    run.py
Author:  Richard Baldwin
Date:    2024
E-mail:  eyeclept@pm.me
Description:
    Early prototype. Unused by the oddarchiver package. Many functions are stubs.
Install Info:

"""
# imports
#import pytermgui as ptg    # terminal ui
import os                  # interact with base system
#import argparse            # parse arguments
import configparser        # parse config file
import logging             # logs things
import subprocess           # used for running commands
import hashlib              # used for hashing
import sys
# constants

# classes

# main function
def main():
    """
    use-----> set INFO logging then return
    input---> none
    output--> none
    details-> does not run an archive
    """
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    pass
# functions
def function():
    """
    use-----> no-op stub
    input---> none
    output--> none
    details-> returns immediately
    """
    pass
def pullConfig():
    """
    use-----> no-op stub
    input---> none
    output--> none
    details-> does not read a config file
    """
    pass
def rsyncTest(source, destination):
    """
    use-----> claimed rsync dry-run. Does not run rsync.
    input---> source, destination
    output--> True
    details-> returns True before the rsync command. Later lines are unreachable.
    """
    return True
    command = ['rsync', '-avn', source, destination]  # "-n" flag for dry-run
    try:
        subprocess.run(command, check=True)
        logging.info("rsync test completed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running rsync test: {e}")

def rsyncCopyOnlyDiffFiles(source, destination):
    """
    use-----> claimed rsync copy. Does not run rsync.
    input---> source, destination
    output--> True
    details-> returns True before the rsync command. Later lines are unreachable.
    """
    return True
    command = ['rsync', '-avz', source, destination]
    try:
        subprocess.run(command, check=True)
        logging.info("rsync completed successfully.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running rsync: {e}")
def setupFolderStructure(backupsDir):
    """
    use-----> no-op stub
    input---> backupsDir
    output--> none
    details-> does not create folders
    """
    # intended: create .tar.gz folder
    # intended: create tmp folder
    pass
def makeFilename(sourceFolderName, isBase = False):
    """
    use-----> build a .tar.gz filename from the source folder name
    input---> sourceFolderName, isBase True for the initial archive
    output--> filename as a str
    details-> appends _base_ or _increment_ plus a unix timestamp. Uses time.time() with no import.
    """
    fileName = sourceFolderName
    if isBase:
        fileName = fileName + "_base_"
    else:
        fileName = fileName + "_increment_"
    fileName = fileName + str(int(time.time())) + ".tar.gz"
    return fileName
def pullODD():
    """
    use-----> no-op stub
    input---> none
    output--> none
    details-> does not pull an optical disc
    """
    pass
def initODD():
    """
    use-----> no-op stub
    input---> none
    output--> none
    details-> returns. The raise below is unreachable.
    """
    pass
    raise ValueError("Something went wrong in my_function")
def appendODD(folderName,bothHddOdd = False, oddLocation = None, hddLocation = None):
    """
    use-----> incomplete append path. Does not write a disc.
    input---> folderName, bothHddOdd, oddLocation, hddLocation
    output--> none
    details-> if bothHddOdd, compares gz lists and raises on a difference.
              then calls pullOdd() (undefined name). Sets test to "". Stops.
    """

    # If bothHddOdd, compare HDD and ODD gz lists
    if bothHddOdd:
        isGzDiff = gzIsDiff(folderName, oddLocation, hddLocation)
        if isGzDiff:
            # gz lists or hashes differ. Log and raise.
            logStr = "HDD and ODD files are different: " + isGzDiff
            logging.error(logStr)
            logStr = "logged- " + logStr
            raise ValueError(logStr)
    # Call pullOdd (name does not match pullODD)
    try:
        pullOdd()    
    except Exception as e:
        logging.error(f"Error running pullOdd: {e}")
    
    # Intended rsync test is not implemented
        # if rsync not diff, return True, no changes
    test = ""
    # Intended: copy rsync diffs without unchanged files


def gzIsDiff(fileHead, oddLocation, hddLocation):
    """
    use-----> compare .tar.gz name lists and hashes in two directories
    input---> fileHead (name prefix), oddLocation, hddLocation
    output--> error text if different, empty string if the lists and hashes match
    details-> hashes each name as a path. Names are not joined to the directory.
    """
    listOfOdd = sorted(listGzFromDir(oddLocation, fileHead))
    listOfHdd = sorted(listGzFromDir(hddLocation, fileHead))
    # check if names match
    if set(listOfHdd) != set(listOfOdd):
        return "elements don't match"
    # check if hashes match
    for i in range(len(listOfOdd)):
        if calculateSha256(listOfOdd[i]) != calculateSha256(listOfHdd[i]):
            returnText = "hashes don't match- oddFile: " + listOfOdd[i] + " hddFile:" + listOfHdd[i]
            return returnText
    # match: return empty string (falsy)
    return ""
        
    pass

def listGzFromDir(dir, fileHead):
    """
    use-----> list .tar.gz names that start with fileHead
    input---> dir to search, fileHead prefix
    output--> list of matching filenames (not full paths)
    details-> uses os.listdir. Does not recurse.
    """
    # vars
    listOfGz = []
    # get the names in dir
    for filename in os.listdir(dir):
        # keep names that start with fileHead and end with .tar.gz
        if filename.startswith(fileHead) and filename.endswith(".tar.gz"):
            listOfGz.append(filename)
    return(listOfGz)
def calculateSha256(filePath):
    """
    use-----> sha256 a file in 4096-byte chunks
    input---> filePath
    output--> hex digest
    details-> opens the path in binary mode
    """
    # Open the file in binary mode
    with open(filePath, 'rb') as file:
        # Read the file in chunks
        chunkSize = 4096
        hashSha256 = hashlib.sha256()
        while True:
            chunk = file.read(chunkSize)
            if not chunk:
                break
            hashSha256.update(chunk)
    
    # Return hex of digest
    return hashSha256.hexdigest()

# prebuiltFuncts
if __name__ == "__main__":
    main()