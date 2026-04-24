"""
File:    .py
Author:  Richard Baldwin
Date:    /2024
E-mail:  eyeclept@pm.me
Description: 
    -
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
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    pass
# functions
def function():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def pullConfig():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def rsyncTest(source, destination):
    """
    use-----> runs rsync test
    input---> 
    output--> 
    details-> 
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
    use-----> 
    input---> 
    output--> 
    details-> 
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
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    #create .tar.gz folder
    #create tmp folder
    pass
def makeFilename(sourceFolderName, isBase = False):
    """
    use-----> returns filename for .tar.gz
    input---> source folder that's being archived,
            isBase is true if this is the initial .tar.gz file
    output--> the fileName as a str
    details-> 
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
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def initODD():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
    raise ValueError("Something went wrong in my_function")
def appendODD(folderName,bothHddOdd = False, oddLocation = None, hddLocation = None):
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    move each diff file into exact folder structure from original and put in “backups” folder
    Turn that into <folderName>_incremental_<dateAndTime>.tar.gz
    Write to ODD
    Delete pulled ODD data        
    """

    #If option to store on both HDD and ODD, Check diff
    if bothHddOdd:
        isGzDiff = gzIsDiff(folderName, oddLocation, hddLocation)
        if isGzDiff:
            #gz is diff, notify and exit
            logStr = "HDD and ODD files are different: " + isGzDiff
            logging.error(logStr)
            logStr = "logged- " + logStr
            raise ValueError(logStr)
    # pull ODD to system
    try:
        pullOdd()    
    except Exception as e:
        logging.error(f"Error running pullOdd: {e}")
    
    #run rsync test
        #if rsync not diff, return True, no changes
    test = ""
    #copy rsync diffs without unchanged


def gzIsDiff(fileHead, oddLocation, hddLocation):
    """
    use-----> compares lists of .gz files in HDD and ODD location
    input---> fileHead (base folder name)
                odd and hdd file locations
    output--> text containing an error if false (use as bool, true if gz is different)
    details-> 
    """
    listOfOdd = sorted(listGzFromDir(oddLocation, fileHead))
    listOfHdd = sorted(listGzFromDir(hddLocation, fileHead))
    # check if elements match
    if set(listOfHdd) != set(listOfOdd):
        return "elements don't match"
    # check if hashes match
    for i in range(len(listOfOdd)):
        if calculateSha256(listOfOdd[i]) != calculateSha256(listOfHdd[i]):
            returnText = "hashes don't match- oddFile: " + listOfOdd[i] + " hddFile:" + listOfHdd[i]
            return returnText
    # if all good return true
    return ""
        
    pass

def listGzFromDir(dir, fileHead):
    """
    use-----> lists only appropriate files
    input---> the dir to search and the fileHead. e.g. backups_whatever_whatever.tar.gz, the word "backups"
    output--> a list of all files matching that description
    details-> 
    """
    #vars
    listOfGz = []
    #get full list in dir
    for filename in os.listdir(dir):
        #filter list and append
        if filename.startswith(fileHead) and filename.endswith(".tar.gz"):
            listOfGz.append(filename)
    return(listOfGz)
def calculateSha256(filePath):
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    # Open the file in binary mode
    with open(filePath, 'rb') as file:
        # Read the file in chunks to handle large files efficiently
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