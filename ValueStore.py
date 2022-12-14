import os
import traceback
import logging
from io import BytesIO

import time

from zeebe_worker import WorkerError

import grpc
from digit_file_mgmt import file_mgmt_pb2, file_mgmt_pb2_grpc

from openpyxl import load_workbook


""" 
Environment
"""
FILE_MGMT_SERVICE = os.getenv('FILE_MGMT_SERVICE',"file-mgmt.worker-services:50051")

SITE_ID = os.getenv('SITE_ID', None)
DRIVE_ID = os.getenv('DRIVE_ID', None)
FOLDER_PATH = os.getenv('FOLDER_PATH', None)

CASH_AGED = 60      # Keep values in cash for 60 seconds

"""
This is the LoneStatistik worker class.
The worker is so far only used in a worker workflow (workflow with a single worker)

The API i described in (the non existent) valuestore_api.yaml

Input header variables are the basic set (documented elseware...)
"""


class ValueStore(object):

    queue_name = "valuestore"        # Name of the Zeebe task queue. Will also influence the worker process ID and name


    """
    Init function. Creates an empty cash
    """
    def __init__(self, async_loop=None):
        self._value_cash = {}                                            # Cash values from previons runs for faster retrieval

        logging.info(f"New valuestore worker using FOLDER_PATH at '{FOLDER_PATH}' with SITE_ID='{SITE_ID}' and DRIVE_ID='{DRIVE_ID}'")


    async def worker(self, vars):
        if 'valueStore' not in vars:
            return {'_DIGIT_ERROR': "valueStore must be given as parameter"}

        file_name = vars['valueStore'] if '.xlsx' in vars['valueStore'] else vars['valueStore'] + ".xlsx"

        if file_name in self._value_cash and time.time()-self._value_cash[file_name]['timestamp'] < CASH_AGED:
            return {'values': self._value_cash[file_name]['values']}        # Return from cash

        try:
            async with grpc.aio.insecure_channel(FILE_MGMT_SERVICE) as channel:
                stub = file_mgmt_pb2_grpc.FileMgmtStub(channel)

                start_time = time.perf_counter()
                req = file_mgmt_pb2.ReadFileRequest(siteId=SITE_ID, driveId=DRIVE_ID, path=FOLDER_PATH, fileName=file_name)
                resp = await stub.ReadFile(req)
                elapsed_time = time.perf_counter() - start_time
                logging.debug(f"File read in {elapsed_time:0.2f} seconds.")

                values = self._read_values(resp.content)         # Read values from value store

        except grpc.aio.AioRpcError as grpc_error:
            # if grpc_error.code() == grpc.StatusCode.NOT_FOUND or grpc_error.code() == grpc.StatusCode.PERMISSION_DENIED:          # Requested valuefile not found
            loggtext = f"Requested valuefile {file_name} not found"
            logging.error(loggtext)
            if '_STANDALONE' in vars:
                return {'_DIGIT_ERROR': loggtext}       # This can be returned to the caller
            else:
                raise WorkerError(loggtext, retries=0)          # Cancel further processeing
        except Exception as e:      # Catch the rest
            logging.fatal(traceback.format_exc(limit=2))
        
        self._value_cash[file_name] = {'timestamp': time.time(), 'values':values}

        return {'values': values}


    def _read_values(self, excel_data):
        wb = load_workbook(BytesIO(excel_data), read_only=True)
        ws = wb.worksheets[0]

        store = {}
        headers = {}
        first_row = True
        for row in ws.rows:
            col = 0

            for cell in row:
                if first_row:
                    key_val = cell.value.strip(u'\u200b')
                    headers[col] = key_val
                    store[key_val] = []
                else:
                    if cell.value:
                        if type(cell.value) is str:
                            store[headers[col]].append(cell.value.strip(u'\u200b'))     # Remove 'ZERO WIDTH SPACE' characters
                        elif type(cell.value) is int:
                            store[headers[col]].append(str(cell.value))     # Convert int to string
                        else:
                            store[headers[col]].append(f"Unhandled type {type(cell.value)} in {cell.coordinate}")     # Unhandled format
                col += 1

            if first_row:
                first_row = False
    
        wb.close()         # Close the workbook after reading

        return store
