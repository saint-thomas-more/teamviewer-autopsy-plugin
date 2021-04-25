"""Python autopsy module to extract and parse Windows TeamViewer artifacts.

This module was designed around original research using TeamViewer 15 on Windows in 2021.

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import os
import re
import time
from datetime import datetime

from java.lang import System
from java.io import File
from java.util import ArrayList
from java.util import NoSuchElementException
from java.util.logging import Level

from org.sleuthkit.datamodel import AbstractFile, ReadContentInputStream, SleuthkitCase
from org.sleuthkit.datamodel import TskCoreException, TskData
from org.sleuthkit.datamodel import BlackboardAttribute, BlackboardArtifact


from org.sleuthkit.autopsy.coreutils import Logger, PlatformUtil
from org.sleuthkit.autopsy.casemodule import Case
from org.sleuthkit.autopsy.casemodule.services import FileManager, Services

from org.sleuthkit.autopsy.ingest import DataSourceIngestModule, IngestModule
from org.sleuthkit.autopsy.ingest import IngestMessage, IngestServices
from org.sleuthkit.autopsy.ingest import IngestModuleFactoryAdapter, IngestModuleIngestJobSettings
from org.sleuthkit.autopsy.ingest import ModuleDataEvent
from org.sleuthkit.autopsy.datamodel import ContentUtils

from com.williballenthin.rejistry import RegistryHiveFile, RegistryKey, RegistryValue
from com.williballenthin.rejistry import RegistryParseException


class TeamViewerIngestModuleFactory(IngestModuleFactoryAdapter):
    """Factory that defines module details and allows Autopsy to create instances for analysis.

    Attributes:
        moduleDisplayName: Module display name.
        moduleDescription: Short description of the module.
        moduleVersionNumber: Current module version number as a String.
    """

    def __init__(self):
        self.settings = None

    moduleDisplayName = "TeamViewer Analyzer Module"
    moduleDescription = "Extracts TeamViewer Artefacts from Windows."
    moduleVersionNumber = "1.0"

    def getModuleDisplayName(self):
        return self.moduleDisplayName

    def getModuleDescription(self):
        return self.moduleDescription

    def getModuleVersionNumber(self):
        return self.moduleVersionNumber

    def isDataSourceIngestModuleFactory(self):
        return True

    def createDataSourceIngestModule(self, ingestOptions):
        return TeamViewerIngestModule(self.settings)


class TeamViewerIngestModule(DataSourceIngestModule):
    """Data source-level ingest module. One created per source.

    Attributes:
        startUpSuccessful: Boolean indicating whether the startup was successful.
            If this is set to False no analysis is performed.
        case: Case object.
        fileManager: File manager object.
        context: Provided context. Unused.
        tempDirPath: Path to the modules local temporary directory.
    """
    LOGGER = Logger.getLogger(TeamViewerIngestModuleFactory.moduleDisplayName)

    HKLM_SOFTWARE_HIVE_FILENAME = "SOFTWARE"
    USER_HIVE_FILENAME = "NTUSER.DAT"

    EXPECTED_HKLM_SOFTWARE_HIVE_FILE_PARENT = "/windows/system32/config/"
    EXPECTED_USER_HIVE_FILE_PARENT = "/users/"

    TEAMVIEWER_REGISTRY_KEY_PATHS = ["TeamViewer", "WOW6432Node/TeamViewer"]

    # Artifact types with display names.
    ARTIFACT_TYPE_DISPLAY_NAME_DICTIONARY = {
        # Registry artifact Types.
        "TSK_TEAMVIEWER_REGISTRY_ITEM": "TeamViewer Registry Items",
        "TSK_TEAMVIEWER_FT_START_DIRECTORY": "TeamViewer File Transfer Directories",
        "TSK_TEAMVIEWER_USERNAME": "TeamViewer Display Names",
        "TSK_TEAMVIEWER_ID": "TeamViewer IDs",
        "TSK_TEAMVIEWER_MAC_ADDRESS": "TeamViewer MAC Addresses",

        # File artifact types.
        "TSK_TEAMVIEWER_LOG": "TeamViewer Log Files",
        "TSK_TEAMVIEWER_SESSION_RECORDING": "TeamViewer Session Recordings",
        "TSK_TEAMVIEWER_CONFIG": "TeamViewer Configuration Files",
        "TSK_TEAMVIEWER_DATABASE": "TeamViewer Databases",

        # Derived artifact types from log files.
        "TSK_TEAMVIEWER_CONNECTION": "TeamViewer Connections",
        "TSK_TEAMVIEWER_PARTICIPANT_ADDED": "TeamViewer Participants Added",
        "TSK_TEAMVIEWER_MEETING_CREATED": "TeamViewer Meetings Created",
        "TSK_TEAMVIEWER_MEETING_PARTICIPANT_ADDED": "TeamViewer Meeting Participant Added",
        "TSK_TEAMVIEWER_AUTHENTICATION_ATTEMPT": "TeamViewer Authentication Failures",
        "TSK_TEAMVIEWER_REMOTE_REBOOT": "TeamViewer Remote Reboots",
        "TSK_TEAMVIEWER_FILE_DOWNLOAD": "TeamViewer File Download Indicators",
        "TSK_TEAMVIEWER_FILE_UPLOAD": "TeamViewer File Upload Indicators",
        "TSK_TEAMVIEWER_IP_ADDRESS": "TeamViewer IP Addresses"
    }

    # File match patterns with descriptions.
    FILE_MATCH_DESCRIPTION_DICTIONARY = {
        "connections_incoming.txt": "Incoming TeamViewer Connections",
        "connections.txt": "Outgoing TeamViewer Connections",
        "TeamViewer%.log": "TeamViewer Program Log",
        "%.tvs": "TeamViewer Session Recording",
        "%.tvc": "TeamViewer Configuration File",
        "tvprint%.db": "TeamViewer Print Database",
        "tvchatfile%.db": "TeamViewer Chat Database"
    }

    # File match patterns with associated artifact types.
    FILE_MATCH_TYPE_DICTIONARY = {
        "connections_incoming.txt": "TSK_TEAMVIEWER_LOG",
        "connections.txt": "TSK_TEAMVIEWER_LOG",
        "TeamViewer%.log": "TSK_TEAMVIEWER_LOG",
        "%.tvs": "TSK_TEAMVIEWER_SESSION_RECORDING",
        "%.tvc": "TSK_TEAMVIEWER_CONFIG",
        "tvprint%.db": "TSK_TEAMVIEWER_DATABASE",
        "tvchatfile%.db": "TSK_TEAMVIEWER_DATABASE"
    }

    TEMP_DIR_NAME = "teamviewer"
    MODULE_NAME = TeamViewerIngestModuleFactory.moduleDisplayName

    startUpSuccessful = None
    tempDirPath = None
    case = None
    fileManager = None
    context = None

    def __init__(self, settings):
        self.context = None

    """Logs messages.

    Given a level and a message, writes to the Autopsy log file.

    Args:
        level: LogLevel enum.
        msg: Message to log.
    """
    @staticmethod
    def log(level, msg):
        TeamViewerIngestModule.LOGGER.logp(level, TeamViewerIngestModuleFactory.moduleDisplayName,
                                           "", msg)

    """Performs initial setup and configuration of the module.

    Retrieves case and file manager objects, creates artifact types and a temporary directory.
    
    startupSuccessful is set to True if, and only if all succeed.

    Args:
        context: Unused.
    """
    def startUp(self, context):
        self.startUpSuccessful = False
        self.context = context
        self.case = Case.getCurrentCase().getSleuthkitCase()
        self.fileManager = Case.getCurrentCase().getServices().getFileManager()

        TeamViewerIngestModule.log(Level.INFO, "Creating new artifact types...")
        for artifactType, displayName in TeamViewerIngestModule.ARTIFACT_TYPE_DISPLAY_NAME_DICTIONARY.items():
            try:
                artifactTypeId = self.case.getArtifactTypeID(artifactType)
                if artifactTypeId == -1:
                    self.case.addArtifactType(artifactType, displayName)
            except TskCoreException as e:
                TeamViewerIngestModule.log(Level.SEVERE, "Failed to create artifact of type %s." %
                                           artifactType)
                TeamViewerIngestModule.log(Level.SEVERE, e.getMessage())
                return

        self.tempDirPath = os.path.join(Case.getCurrentCase().getTempDirectory(),
                                        TeamViewerIngestModule.TEMP_DIR_NAME)
        TeamViewerIngestModule.log(Level.INFO, "Creating temporary directory %s." %
                                   self.tempDirPath)

        if not os.path.isdir(self.tempDirPath):
            try:
                os.mkdir(self.tempDirPath)
            except os.error:
                TeamViewerIngestModule.log(Level.SEVERE, "Failed to create temporary directory.")
                return

        self.startUpSuccessful = True
        TeamViewerIngestModule.log(Level.INFO, "Startup successful.")

    """Performs analysis of a data source.

   Files of interest are found parsed and relevant artifacts created.

    Args:
        dataSource: Data source to operate on.
        progressBar: ProgressBar object to post updates to.
        
    Returns:
        IngestModule.ProcessResult detailing whether analysis succeeded.
    """
    def process(self, dataSource, progressBar):
        if not self.startUpSuccessful:
            TeamViewerIngestModule.log(Level.SEVERE, "StartUp was unsuccessful, terminating.")
            message = IngestMessage.createMessage(IngestMessage.MessageType.DATA,
                                                  "TeamViewer",
                                                  "Failed to analyse TeamViewer files, " +
                                                  "see log for details.")
            IngestServices.getInstance().postMessage(message)
            return IngestModule.ProcessResult.ERROR

        TeamViewerIngestModule.log(Level.INFO, "Beginning analysis.")
        progressBar.switchToIndeterminate()

        # Locate files of interest.
        localMachineSoftwareHiveFileList = self.fileManager.findFiles(dataSource,
                                          TeamViewerIngestModule.HKLM_SOFTWARE_HIVE_FILENAME,
                                          TeamViewerIngestModule.EXPECTED_HKLM_SOFTWARE_HIVE_FILE_PARENT)
        userHiveFileList = self.fileManager.findFiles(dataSource,
                                          TeamViewerIngestModule.USER_HIVE_FILENAME,
                                          TeamViewerIngestModule.EXPECTED_USER_HIVE_FILE_PARENT)

        hiveFileList = localMachineSoftwareHiveFileList + userHiveFileList

        # Create a dictionary of File IDs to match strings to count files and record
        # results, therefore not requiring a further search later.
        filesystemArtifactFileIdMatchDictionary = {}
        for match in TeamViewerIngestModule.FILE_MATCH_DESCRIPTION_DICTIONARY:
            for abstractFile in self.fileManager.findFiles(dataSource, match):
                filesystemArtifactFileIdMatchDictionary[abstractFile.getId()] = match

        filesToProcessCount = len(hiveFileList) + len(filesystemArtifactFileIdMatchDictionary)
        filesProcessedCount = 0

        TeamViewerIngestModule.log(Level.INFO, "A total of %d files to process have been found."
                                   % filesToProcessCount)
        progressBar.switchToDeterminate(filesToProcessCount)

        # Process Registry Hives.
        for hiveFile in hiveFileList:
            TeamViewerIngestModule.log(Level.INFO, "Found relevant Registry Hive at %s." %
                                       hiveFile.getUniquePath())
            self.processHive(hiveFile)
            filesProcessedCount += 1
            progressBar.progress(filesProcessedCount)

        # Process Filesystem artifacts.
        TeamViewerIngestModule.log(Level.INFO, "Processing Filesystem artifacts.")
        for fileId, match in filesystemArtifactFileIdMatchDictionary.items():
            abstractFile = self.case.getAbstractFileById(fileId)
            artifact = abstractFile.newArtifact(
                self.case.getArtifactTypeID(
                    TeamViewerIngestModule.FILE_MATCH_TYPE_DICTIONARY[match]))

            attributes = ArrayList()

            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DESCRIPTION,
                TeamViewerIngestModule.FILE_MATCH_DESCRIPTION_DICTIONARY[match]))
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_KEYWORD_REGEXP, match))
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_PATH, abstractFile.getUniquePath()))

            artifact.addAttributes(attributes)

            # Extract specific details from relevant artifacts.
            if "connections" in match:
                self.processConnectionFile(abstractFile, artifact)
            elif match == "TeamViewer%.log":
                self.processLogFile(abstractFile, artifact)
            elif match == "%.tvs":
                self.processSessionFile(abstractFile, artifact)
            elif match == "%.tvc":
                self.processConfigFile(abstractFile, artifact)
            filesProcessedCount += 1
            progressBar.progress(filesProcessedCount)

        TeamViewerIngestModule.log(Level.INFO, "Files processed.")
        return IngestModule.ProcessResult.OK

    """Creates a temporary file.

   Attempts to create a temporary file in the temporary directory, using the unique ID
   as a filename.

    Args:
        abstractFile: AbstractFile to write to disk.
        
    Returns:
        The created file's path.
    """
    def createTemporaryFile(self, abstractFile):
        filePath = os.path.join(self.tempDirPath, str(abstractFile.getId()))
        ContentUtils.writeToFile(abstractFile, File(filePath))
        return filePath

    """Processes a Registry Hive.
    
    Given an identified Registry Hive file, finds TeamViewer-related keys and produces relevant
    artifacts.
    
     Args:
         abstractFile: AbstractFile to process.
     """
    def processHive(self, abstractFile):
        TeamViewerIngestModule.log(Level.INFO, "Processing %s." % abstractFile.getUniquePath())
        filePath = self.createTemporaryFile(abstractFile)
        registryHiveFile = RegistryHiveFile(File(filePath))

        for registryKeyPath in TeamViewerIngestModule.TEAMVIEWER_REGISTRY_KEY_PATHS:
            if abstractFile.getUniquePath().endswith(TeamViewerIngestModule.USER_HIVE_FILENAME):
                registryKeyPath = "SOFTWARE/" + registryKeyPath
            regKeyList = registryKeyPath.split('/')
            currentRegistryKey = registryHiveFile.getRoot()
            TeamViewerIngestModule.log(Level.INFO, "Searching Hive for %s." % registryKeyPath)
            try:
                for key in regKeyList:
                    currentRegistryKey = currentRegistryKey.getSubkey(key)
            except (RegistryParseException, NoSuchElementException):
                TeamViewerIngestModule.log(Level.INFO, "Failed to find %s in Hive."
                                           % registryKeyPath)
                continue
            registryNameValueDictionary = TeamViewerIngestModule.processRegistryTree(currentRegistryKey)
            self.createRegistryArtifacts(registryNameValueDictionary, abstractFile)

        TeamViewerIngestModule.log(Level.INFO, "Finished processing %s." %
                                   abstractFile.getUniquePath())

    """Creates Autopsy artifacts based upon retrieved Registry values.
    
    Args:
        registryNameValueDictionary: Dictionary of RegistryKey names to value Strings.
        abstractFile: AbstractFile of the relevant Hive from which values came.
     """
    def createRegistryArtifacts(self, registryNameValueDictionary, abstractFile):
        for keyName, keyValue in registryNameValueDictionary.items():
            # First add the artifact as a basic REGISTRY_ITEM.
            artifact = abstractFile.newArtifact(
                self.case.getArtifactTypeID("TSK_TEAMVIEWER_REGISTRY_ITEM"))
            attributes = ArrayList()

            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_NAME, keyName))
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_VALUE, keyValue))
            artifact.addAttributes(attributes)

            # Then extract and parse more specific information.
            self.identifyIpAddresses(abstractFile, keyValue, "Extracted from %s Registry key." %
                                     keyName,
                                     artifact)

            attributes = ArrayList()
            originalArtifact = artifact
            if keyName == "InstallationDirectory":
                artifact = abstractFile.newArtifact(
                    BlackboardArtifact.ARTIFACT_TYPE.TSK_INSTALLED_PROG)

                attributes.add(TeamViewerIngestModule.createAttribute(
                    BlackboardAttribute.ATTRIBUTE_TYPE.TSK_PROG_NAME, "TeamViewer"))
                attributes.add(TeamViewerIngestModule.createAttribute(
                    BlackboardAttribute.ATTRIBUTE_TYPE.TSK_PATH, keyValue))

                if "Version" in registryNameValueDictionary:
                    attributes.add(TeamViewerIngestModule.createAttribute(
                        BlackboardAttribute.ATTRIBUTE_TYPE.TSK_VERSION,
                        registryNameValueDictionary["Version"]))

            elif keyName == "ClientID":
                self.createTeamViewerIdArtefact(abstractFile, keyValue,
                                                "ID of the Client from Registry",
                                                originalArtifact)
                continue
            elif keyName == "FT_Start_Directories":
                # Entries are of the form <SlaveID>?<Local Directory>|<Remote Directory>
                # e.g. 958223731?C:\Users\Controller\Desktop|C:/Users/Slave/Desktop
                # Entries are comma-separated and as strings in square brackets

                # Remove square brackets
                keyValue = keyValue.replace("[", "")
                keyValue = keyValue.replace("]", "")

                # Extract each entry, considering only those that are long enough to contain information
                entries = keyValue.split(",")
                for e in entries:
                    if len(e) > 5:
                        attributes = ArrayList()
                        extractedId = ""
                        localPath = ""
                        remotePath = ""

                        extractedIdList = re.findall("[0-9]+", e)
                        if len(extractedIdList) > 0:
                            extractedId = extractedIdList[0]

                        extractedPaths = e.split("|")
                        if len(extractedPaths) > 1:
                            remotePath = extractedPaths[1]
                            localPath = extractedPaths[0].split("?")[1]

                        artifact = abstractFile.newArtifact(self.case.getArtifactTypeID(
                            "TSK_TEAMVIEWER_FT_START_DIRECTORY"))

                        attributes.add(TeamViewerIngestModule.createAttribute(
                            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_USER_ID, extractedId))
                        attributes.add(TeamViewerIngestModule.createAttribute(
                            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_LOCAL_PATH, localPath))
                        attributes.add(TeamViewerIngestModule.createAttribute(
                            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_REMOTE_PATH, remotePath))
                        attributes.add(TeamViewerIngestModule.createAttribute(
                            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_ASSOCIATED_ARTIFACT,
                            originalArtifact.getArtifactID()))

                        artifact.addAttributes(attributes)

                        if not extractedId == "":
                            self.createTeamViewerIdArtefact(abstractFile,
                                                            extractedId,
                                                            "Extracted from File Transfer Registry Key",
                                                            artifact)
                continue
            elif keyName == "Username" or keyName == "Meeting_UserName":

                if keyName == "Meeting_UserName":
                    description = "Meeting Username"
                else:
                    description = "Display Name"
                self.createTeamViewerUsernameArtefact(abstractFile, keyValue,
                                                      description + " from Registry", artifact)
            elif keyName == "LastMACUsed":
                entries = re.findall("[0-9A-Fa-f]{12}", keyValue)
                for e in entries:
                    attributes = ArrayList()
                    artifact = abstractFile.newArtifact(self.case.getArtifactTypeID(
                        "TSK_TEAMVIEWER_MAC_ADDRESS"))

                    attributes.add(TeamViewerIngestModule.createAttribute(
                        BlackboardAttribute.ATTRIBUTE_TYPE.TSK_VALUE, e))
                    attributes.add(TeamViewerIngestModule.createAttribute(
                        BlackboardAttribute.ATTRIBUTE_TYPE.TSK_ASSOCIATED_ARTIFACT,
                        originalArtifact.getArtifactID()))

                    artifact.addAttributes(attributes)
                continue
            else:
                continue
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_ASSOCIATED_ARTIFACT,
                originalArtifact.getArtifactID()))
            artifact.addAttributes(attributes)

    """Processes a TeamViewer log file.
    
    Given a potential TeamViewer log file, identifies relevant data and creates new artifacts.
    
     Args:
         abstractFile: AbstractFile to process.
         artifact: Existing parent artifact.
     """
    def processLogFile(self, abstractFile, artifact):
        filePath = self.createTemporaryFile(abstractFile)
        with open(filePath, "r") as fp:
            for line in fp:
                self.identifyIpAddresses(abstractFile, line, "Extracted from log file.",
                                         artifact)
                #TODO Reboot
                #TODO Meeting
                #TODO

    """Processes a TeamViewer session file.
    
    Given a potential TeamViewer session file, validates it and identifies relevant data and 
    creates new artifacts.
    
     Args:
         abstractFile: AbstractFile to process.
         artifact: Existing parent artifact.
     """
    def processSessionFile(self, abstractFile, artifact):
        filePath = self.createTemporaryFile(abstractFile)
        with open(filePath, "r") as fp:
            line = fp.readline()
            if line == "TVS":
                for _ in range(6):
                    line = fp.readline()
                    self.identifyIpAddresses(abstractFile, line,
                                             "Extracted from session file.", artifact)
                    if line == "":
                        return
                    lineValues = line.split()
                    if len(lineValues) < 1:
                        continue
                    fieldName = lineValues[0]
                    description = fieldName + " extracted from Session File"
                    if len(lineValues) == 2 and fieldName == "ClientID":
                        self.createTeamViewerIdArtefact(abstractFile, lineValues[1],
                                                        description, artifact)
                    elif len(lineValues) >= 2 and fieldName == "ServerID":
                        extractedData = line.replace("ServerID", "")
                        extractedData = extractedData.strip()
                        self.createTeamViewerIdArtefact(abstractFile, extractedData,
                                                        description, artifact)
                        self.createTeamViewerUsernameArtefact(abstractFile, extractedData,
                                                              description, artifact)
            else:
                TeamViewerIngestModule.log(Level.WARNING, "Not a valid Session File: %s." %
                                           abstractFile.getUniquePath())

    """Processes a TeamViewer configuration file.
    
    Given a potential TeamViewer configuration file, validates it and identifies relevant data and 
    creates new artifacts.
    
     Args:
         abstractFile: AbstractFile to process.
         artifact: Existing parent artifact.
     """
    def processConfigFile(self, abstractFile, artifact):
        filePath = self.createTemporaryFile(abstractFile)
        with open(filePath, "r") as fp:
            line = fp.readline()
            if "[TeamViewer Configuration]" in line:
                line = fp.readline()
                self.identifyIpAddresses(abstractFile, line, "Extracted from configuration file.",
                                         artifact)
                data = line.split("=")
                if len(data) == 2 and data[0] == "targetID":
                    extractedId = data[1]
                    if extractedId.startswith("m"):
                        description = "Meeting ID extracted from Configuration File"
                    else:
                        description = "Slave ID extracted from Configuration File"
                    self.createTeamViewerIdArtefact(abstractFile, extractedId,
                                                    description, artifact)
            else:
                TeamViewerIngestModule.log(Level.INFO, "Not a valid Configuration File: %s." %
                         abstractFile.getUniquePath())

    """Processes a TeamViewer connection file.
    
    Given a potential TeamViewer connection file, validates it and identifies relevant data and 
    creates new artifacts.
    
     Args:
         abstractFile: AbstractFile to process.
         artifact: Existing parent artifact.
     """
    def processConnectionFile(self, abstractFile, artifact):
        filePath = self.createTemporaryFile(abstractFile)
        with open(filePath, "r") as fp:
            for line in fp:
                self.identifyIpAddresses(abstractFile, line, "Extracted from connection file.",
                                         artifact)
                valueList = line.split()
                if len(valueList) > 7:
                    self.createTeamViewerConnectionArtefacts(abstractFile, valueList, artifact)

    """Creates a new TeamViewer ID Artifact.
    
     Args:
         abstractFile: AbstractFile of artifact.
         value: TeamViewer ID.
         description: Description to give the artifact.
         associatedArtifact: Associated parent Artifact.
     """
    def createTeamViewerIdArtefact(self, abstractFile, value, description, associatedArtifact):
        self.createBasicArtifact("TSK_TEAMVIEWER_ID", abstractFile,
                                 value, description, associatedArtifact)

    """Creates a new TeamViewer Username Artifact.
    
     Args:
         abstractFile: AbstractFile of artifact.
         value: TeamViewer username.
         description: Description to give the artifact.
         associatedArtifact: Associated parent Artifact.
     """
    def createTeamViewerUsernameArtefact(self, abstractFile, value, description, associatedArtifact):
        self.createBasicArtifact("TSK_TEAMVIEWER_USERNAME", abstractFile,
                                                   value, description, associatedArtifact)

    """Searches for IPv4 addresses in a String and creates relevant artifacts.
    
     Args:
         abstractFile: AbstractFile of artifact.
         value: Value to search for IPv4 addresses.
         description: Description to give any found addresses.
         associatedArtifact: Associated parent Artifact.
     """
    def identifyIpAddresses(self, abstractFile, value, description, associatedArtifact):
        regularExpressionString = r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
        extractedAddresses = re.findall(regularExpressionString, value)
        for address in extractedAddresses:
            createdArtifact = self.createBasicArtifact("TSK_TEAMVIEWER_IP_ADDRESS", abstractFile,
                                 address, description, associatedArtifact)
            attributes = ArrayList()

            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_KEYWORD_REGEXP, regularExpressionString))
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_TEXT, value))
            createdArtifact.addAttributes(attributes)

    """Creates a new TeamViewer Artifact with basic values.
    
     Args:
        artifactTypeName: Type name of the artifact.
        abstractFile: AbstractFile of artifact.
        value: Artifact value.
        description: Description to give the artifact.
        associatedArtifact: Associated parent Artifact.
        
    Returns:
        Created Artifact object.
     """
    def createBasicArtifact(self, artifactTypeName, abstractFile, value, description,
                            associatedArtifact):
        artifact = abstractFile.newArtifact(self.case.getArtifactTypeID(artifactTypeName))
        attributes = ArrayList()

        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_VALUE, value))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DESCRIPTION, description))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_ASSOCIATED_ARTIFACT,
            associatedArtifact.getArtifactID()))

        artifact.addAttributes(attributes)
        return artifact

    """Creates new TeamViewer Connection Artifacts.
    
    This function takes a list of values extracted from a line of a connection file and depending
    upon the specific file will attempt to parse the values and create relevant artifacts.
    
     Args:
        abstractFile: AbstractFile of artifact.
        valueList: List of values to parse.
        associatedArtifact: Associated parent Artifact.
     """
    def createTeamViewerConnectionArtefacts(self, abstractFile, valueList, associatedArtifact):
        # Lines are of the format:
        # Outgoing:
        # <Slave ID> <Start Date> <Start Time> <End Date> <End Time>
        # <Current Windows User> <Connection Type> <Unique Session ID>

        # Incoming:
        # <Controller ID> <Controller Display Name> <Start Date> <Start Time> <End Date> <End Time>
        # <Current Windows User> <Connection Type> <Unique Session ID>

        if "incoming" in abstractFile.getUniquePath():
            direction = "incoming"
        else:
            direction = "outgoing"

        if not valueList[0].isdigit():
            return
        extractedId = valueList[0]
        self.createTeamViewerIdArtefact(abstractFile, extractedId,
                                        "Extracted from %s connection File." % direction,
                                        associatedArtifact)
        if direction == "outgoing":
            startString = "%s %s" % (valueList[1], valueList[2])
            endString = "%s %s" % (valueList[3], valueList[4])
            connectionType = valueList[6]
        else:
            self.createTeamViewerUsernameArtefact(abstractFile, valueList[1],
                                                  "Extracted from incoming connection file",
                                                  associatedArtifact)
            startString = "%s %s" % (valueList[2], valueList[3])
            endString = "%s %s" % (valueList[4], valueList[5])
            connectionType = valueList[7]

        artifact = abstractFile.newArtifact(self.case.getArtifactTypeID("TSK_TEAMVIEWER_CONNECTION"))
        attributes = ArrayList()

        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DIRECTION,
            direction))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_ASSOCIATED_ARTIFACT,
            associatedArtifact.getArtifactID()))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_USER_ID,
            extractedId))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DESCRIPTION,
            "%s %s connection with %s from %s until %s." % (direction.capitalize(), connectionType,
                                                            extractedId, startString, endString)))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_TEXT, "|".join(valueList)))
        attributes.add(TeamViewerIngestModule.createAttribute(
            BlackboardAttribute.ATTRIBUTE_TYPE.TSK_TEXT, "|".join(valueList)))

        try:
            start = datetime.strptime(startString, "%d-%m-%Y %H:%M:%S")
            end = datetime.strptime(endString, "%d-%m-%Y %H:%M:%S")

            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DATETIME_START,
                int(time.mktime(start.timetuple()))))
            attributes.add(TeamViewerIngestModule.createAttribute(
                BlackboardAttribute.ATTRIBUTE_TYPE.TSK_DATETIME_END,
                int(time.mktime(end.timetuple()))))
        except ValueError:
            TeamViewerIngestModule.log(Level.WARNING, "Failed to extract times from connection in "
                                                      "line %s." % "|".join(valueList))

        artifact.addAttributes(attributes)

    """Returns Registry data from all subkeys and values of a RegistryKey.
    
    From a RegistryKey all values are extracted and converted to Strings and the same is 
    recursively done for all subkeys.
    
    Args:
        registryKey: Root RegistryKey to process from.
        registryNameValueDictionary: 
    Returns:
        Dictionary of Registry key names to their values as Strings.
     """
    @staticmethod
    def processRegistryTree(registryKey, registryNameValueDictionary=None):
        if registryNameValueDictionary is None:
            registryNameValueDictionary = {}
        # currentKey now holds the relevant TeamViewer parent key.
        for value in registryKey.getValueList():
            registryNameValueDictionary[value.getName()] = TeamViewerIngestModule.getRegistryValueAsString(value)

        for subkey in registryKey.getSubkeyList():
            registryNameValueDictionary.update(
                TeamViewerIngestModule.processRegistryTree(subkey, registryNameValueDictionary))
        return registryNameValueDictionary

    """Converts a RegistryValue object to a String.
    
    If the object cannot be simply converted a binary representation is created
    
    Args:
        registryValueObject: RegistryValue object to convert.
         
    Returns:
        String representation of registryValueObject.
     """
    @staticmethod
    def getRegistryValueAsString(registryValueObject):
        registryValueDataObject = registryValueObject.getValue()
        valueTypeObject = registryValueDataObject.getValueType()
        valueTypeString = valueTypeObject.toString()
        if valueTypeString == "REG_EXPAND_SZ" or valueTypeString == "REG_SZ":
            return registryValueDataObject.getAsString()
        elif valueTypeString == "REG_MULTI_SZ":
            return registryValueDataObject.getAsStringList().toString()
        elif valueTypeString == "REG_DWORD" or valueTypeString == "REG_QWORD":
            return str(registryValueDataObject.getAsNumber())
        else:
            # We don't know if the ByteBuffer is array-backed so just read the raw values
            rawData = registryValueDataObject.getAsRawData()
            arrayLength = rawData.remaining()
            stringData = "Raw: "
            for _ in range(0, arrayLength):
                byte = rawData.get()
                if byte < 0:
                    byte += 256
                stringData = stringData + " " + format(byte, '02x')
            return stringData.strip()

    """Creates an Autopsy BlackboardAttribute.
    
    Args:
        attributeType: Attribute type constant.
        value: Value for the attribute.
        
    Returns:
        BlackboardAttribute of attributeType with the specified value.
     """
    @staticmethod
    def createAttribute(attributeType, value):
        return BlackboardAttribute(attributeType.getTypeID(),
                                   TeamViewerIngestModuleFactory.moduleDisplayName, value)