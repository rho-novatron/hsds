#
# common node methods of hsds cluster
# 
import asyncio
import json
import time
import sys

from aiohttp.web import Application, Response, StreamResponse, run_app
from aiohttp import ClientSession, TCPConnector 
from aiohttp.errors import HttpBadRequest, ClientError
import aiobotocore
 

import config
from timeUtil import unixTimeToUTC, elapsedTime
from hsdsUtil import http_get_json, isOK, createNodeId, http_post, jsonResponse
import hsds_logger as log


async def register(app):
    """ register node with headnode
    OK to call idempotently (e.g. if the headnode seems to have forgotten us)"""

    req_reg = app["head_url"] + "/register"
    log.info("register: {}".format(req_reg))
   
    body = {"id": app["id"], "port": app["node_port"], "node_type": app["node_type"]}
    try:
        log.info("register req: {} body: {}".format(req_reg, body))
        rsp_json = await http_post(app, req_reg, body)
        print("rsp_json:", rsp_json)       
        if rsp_json is not None:
            log.info("register response: {}".format(rsp_json))
            app["node_number"] = rsp_json["node_number"]
            app["node_count"] = rsp_json["node_count"]
            log.info("setting node_state to WAITING")
            app["node_state"] = "WAITING"  # wait for other nodes to be active
    except OSError:
        log.error("failed to register")


async def healthCheck(app):
    """ Periodic method that either registers with headnode (if state in INITIALIZING) or 
    calls headnode to verify vitals about this node (otherwise)"""
    log.info("health check start")
    sleep_secs = config.get("node_sleep_time")
    while True:
        if app["node_state"] == "INITIALIZING":
            await register(app)
        else:
            # check in with the head node and make sure we are still active
            req_node = "{}/nodestate".format(app["head_url"])
            log.info("health check req {}".format(req_node))
            try:
                rsp_json = await http_get_json(app, req_node)
                if rsp_json is None or not isinstance(rsp_json, dict):
                    log.warn("invalid health check response: type: {} text: {}".format(type(rsp_json), rsp_json))
                else:
                    #print("rsp_json: ", rsp_json)
                    # save the url's to each of the active nodes'
                    sn_urls = {}
                    dn_urls = {}
                    #  or rsp_json["host"] is None or rsp_json["id"] != app["id"]
                    for node in rsp_json["nodes"]:
                        if node["node_type"] == app["node_type"] and node["node_number"] == app["node_number"]:
                            # this should be this node
                            if node["id"] != app["id"]:
                                # flag - to re-register
                                log.warn("mis-match node ids, app: {} vs head: {} - re-initializing".format(node["id"], app["id"]))
                                app["node_state"] == "INITIALIZING"
                        if not node["host"]:
                            continue  # not online
                        url = "http://" + node["host"] + ":" + str(node["port"])
                        node_number = node["node_number"]
                        if node["node_type"] == "dn":
                            dn_urls[node_number] = url
                        else: 
                            sn_urls[node_number] = url
                    app["sn_urls"] = sn_urls
                    app["dn_urls"] = dn_urls
                    cluster_state = rsp_json["cluster_state"]
                    log.info("Cluster_state: {}".format(cluster_state))
                    if app["node_state"] == "WAITING" and cluster_state == "READY":
                        log.info("setting node_state to READY")
                        app["node_state"]  = "READY"
                    elif app["node_state"] == "READY" and cluster_state != "READY":
                        log.info("setting node_state to WAITING")
                        app["node_state"]  = "WAITING"
                        
                    log.info("health check ok") 
            except ClientError as ce:
                log.warn("ClientError: {} for health check".format(str(ce)))

        log.info("health check sleep: {}".format(sleep_secs))
        await asyncio.sleep(sleep_secs)
 
async def info(request):
    """HTTP Method to retun node state to caller"""
    log.request(request)
    app = request.app
    resp = StreamResponse()
    resp.headers['Content-Type'] = 'application/json'
    answer = {}
    # copy relevant entries from state dictionary to response
    answer['id'] = request.app['id']
    answer['node_type'] = request.app['node_type']
    answer['start_time'] = unixTimeToUTC(app['start_time'])
    answer['up_time'] = elapsedTime(app['start_time'])
    answer['node_state'] = app['node_state'] 
    answer['node_number'] = app['node_number']
    answer['node_count'] = app['node_count']
        
    resp = await jsonResponse(request, answer) 
    log.response(request, resp=resp)
    return resp


def baseInit(loop, node_type):
    """Intitialize application and return app object"""
    log.info("Application baseInit")
    app = Application(loop=loop)

    # set a bunch of global state 
    app["id"] = createNodeId(node_type)
    app["node_state"] = "INITIALIZING"
    app["node_type"] = node_type
    app["node_port"] = config.get(node_type + "_port")
    app["node_number"] = -1
    app["node_count"] = -1
    app["start_time"] = int(time.time())  # seconds after epoch
    app["bucket_name"] = config.get("bucket_name")
    app["head_url"] = "http://{}:{}".format(config.get("head_host"), config.get("head_port"))
    app["sn_urls"] = {}
    app["dn_urls"] = {}

    # create a client Session here so that all client requests 
    #   will share the same connection pool
    max_tcp_connections = int(config.get("max_tcp_connections"))
    client = ClientSession(loop=loop, connector=TCPConnector(limit=max_tcp_connections))

    # get connection to S3
    # app["bucket_name"] = config.get("bucket_name")
    aws_region = config.get("aws_region")
    aws_secret_access_key = config.get("aws_secret_access_key")
    if not aws_secret_access_key or aws_secret_access_key == 'xxx':
        msg="Invalid aws secret access key"
        log.error(msg)
        sys.exit(msg)
    aws_access_key_id = config.get("aws_access_key_id")
    if not aws_access_key_id or aws_access_key_id == 'xxx':
        msg="Invalid aws access key"
        log.error(msg)
        sys.exit(msg)

    session = aiobotocore.get_session(loop=loop)
    aws_client = session.create_client('s3', region_name=aws_region,
                                   aws_secret_access_key=aws_secret_access_key,
                                   aws_access_key_id=aws_access_key_id)
    app['client'] = client
    app['s3'] = aws_client

    app.router.add_get('/', info)
    app.router.add_get('/info', info)
      
    return app
 