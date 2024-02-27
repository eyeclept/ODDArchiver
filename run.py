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
#import os                  # interact with base system
#import argparse            # parse arguments
import configparser        # parse config file
#import logging             # logs things
#import paramiko            # connect to ssh
#import numpy as np         # does math thigs

from pyudf.filesystem import UDFImage #work with ODD filesystem
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
def rsyncTest():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def rsyncRun():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def diffGz():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def setupFolderStructure():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def extractGz():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def makeFilename():
    """
    use-----> 
    input---> 
    output--> 
    details-> 
    """
    pass
def initUDF(imagePath):
    """
    use-----> 
    input---> 
    output--> 
    details-> 
        probably need pyudf for universal disk format
        

        test code below
    """

    udfImage = UDFImage.new(imagePath, allow_multi_session=True)
    udfImage.close()
    
def appendUDF(imagePath, filePath):
    """
    use-----> 
    input---> 
    output--> 
    details-> 
        test code below
    """
    
    # Mount the existing UDF image with multi-session support
    udfImage = UDFImage.open(imagePath, allow_multi_session=True)
    
    # Add a new file to the UDF image
    with open(filePath, 'rb') as file:
        udfImage.add_file(os.path.basename(filePath), file.read())

    udfImage.close()
# prebuiltFuncts
if __name__ == "__main__":
    main()