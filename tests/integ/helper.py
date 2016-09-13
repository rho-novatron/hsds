##############################################################################
# Copyright by The HDF Group.                                                #
# All rights reserved.                                                       #
#                                                                            #
# This file is part of HSDS (HDF5 Scalable Data Service), Libraries and      #
# Utilities.  The full HSDS copyright notice, including                      #
# terms governing use, modification, and redistribution, is contained in     #
# the file COPYING, which can be found at the root of the source code        #
# distribution tree.  If you do not have access to this file, you may        #
# request a copy from help@hdfgroup.org.                                     #
##############################################################################
import requests
import json
import config
"""
    Helper function - get endpoint we'll send http requests to 
""" 
def getEndpoint(node_type):
    
    endpoint = 'http://' + config.get(node_type + '_host') + ':' + str(config.get(node_type + '_port'))
    return endpoint

"""
Helper function - return true if the parameter looks like a UUID
"""
def validateId(id):
    if type(id) != str and type(id) != unicode: 
        # should be a string
        return False
    if len(id) != 36:
        # id's returned by uuid.uuid1() are always 36 chars long
        return False
    return True

"""
Helper - return number of active sn/dn nodes
"""
def getActiveNodeCount():
    req = getEndpoint("head") + "/info"
    rsp = requests.get(req)   
    rsp_json = json.loads(rsp.text)
    print("rsp_json", rsp_json)
    sn_count = rsp_json["active_sn_count"]
    dn_count = rsp_json["active_dn_count"]
    return sn_count, dn_count
        
