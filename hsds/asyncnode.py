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
#
# Head node of hsds cluster
# 
import asyncio
import time

from aiohttp.web import run_app
from aiohttp.errors import HttpProcessingError, HttpBadRequest

import config
from basenode import baseInit, healthCheck
from util.timeUtil import unixTimeToUTC
from util.s3Util import putS3Bytes, isS3Obj, getS3JSONObj, getS3ObjStats
from util.idUtil import getCollectionForId, getS3Key, getDataNodeUrl, isValidChunkId, isValidUuid
from util.domainUtil import isValidDomain
from util.httpUtil import http_delete, jsonResponse, StreamResponse
from util.chunkUtil import getDatasetId 
from asyncnode_lib import listKeys, markObj
import hsds_logger as log
 
FORCE_CONTENT_LIST_CREATION = True
 
async def updateDatasetContents(app, domain, dsetid):
    """ Create a object listing all the chunks for given dataset
    """
    log.info("updateDatasetContents: {}".format(dsetid))
    datasets = app["datasets"]
    if dsetid not in datasets:
        log.error("expected to find dsetid")
        return
    dset_obj = datasets[dsetid]
    chunks = dset_obj["chunks"]
    if len(chunks) == 0:
        log.info("no chunks for dataset")
        return
    # TBD: Replace with domainUtil func
    col_s3key = domain[1:] + "/." + dsetid + ".chunks.txt"  
    if await isS3Obj(app, col_s3key):
        # contents already exist, return
        # TBD: Add an option to force re-creation of index?
        if not FORCE_CONTENT_LIST_CREATION:
            return
         
    chunk_ids = list(chunks.keys())
    chunk_ids.sort()
    text_data = b""
    for chunk_id in chunk_ids:
        log.info("getting chunk_obj for {}".format(chunk_id))
        chunk_obj = chunks[chunk_id]
        # chunk_obj should have keys: ETag, Size, and LastModified 
        if "ETag" not in chunk_obj:
            log.warn("chunk_obj for {} not initialized".format(chunk_id))
            continue
        line = "{} {} {} {}\n".format(chunk_id[39:], chunk_obj["ETag"], chunk_obj["LastModified"], chunk_obj["Size"])
        log.info("chunk contents: {}".format(line))
        line = line.encode('utf8')
        text_data += line
    log.info("write chunk collection key: {}, count: {}".format(col_s3key, len(chunk_ids)))
    try:
        await putS3Bytes(app, col_s3key, text_data)
    except HttpProcessingError:
        log.error("S3 Error writing chunk collection key: {}".format(col_s3key))
  

async def updateDomainContent(app, domain, objs_updated=None):
    """ Create/update context files listing objids and size for objects in the domain.
    """
    log.info("updateDomainContent: {}".format(domain))
     
    domains = app["domains"]
    log.info("{} domains".format(len(domains)))
    if objs_updated is not None:
        log.info("objs_updated: {}".format(objs_updated))
     
    domain_obj = domains[domain]
    # for folder objects, the domain_obj won't have a groups key
    if "root" not in domain_obj:
        log.info("Folder domain skipping: {}".format(domain))
        return  # just a folder domain
    rootid = domain_obj["root"]
    roots = app["roots"]
    if rootid not in roots:
        log.warn("expected to find root: {} in roots collection".format(rootid))
        return
    root_obj = roots[rootid]
    for collection in ("groups", "datatypes", "datasets"):
        # if objs_updated is passed in, check that at least one of the relevant object types
        # is included, otherwise skip
        if objs_updated is not None:
            collection_included = False
            for objid in objs_updated:
                if isValidChunkId(objid) and collection == "datasets":
                    # if a chunk is updated, recalculate the datasets list
                    collection_included = True
                    break
                elif getCollectionForId(objid) == collection:
                    collection_included = True
                    break
            if not collection_included:
                log.info("no updated for collection: {}".format(collection))
                continue  # go on to next collection
        domain_col = root_obj[collection]
        log.info("domain_{} count: {}".format(collection, len(domain_col)))
        log.info("domain_{} items: {}".format(collection, domain_col))
        col_s3key = domain[1:] + "/." + collection + ".txt"  
        if await isS3Obj(app, col_s3key):
            # Domain collection already exist
            # TBD: add option to force re-creation?
            if not FORCE_CONTENT_LIST_CREATION:
                continue
        if len(domain_col) > 0:
            log.info("updating domain collection: {} for domain: {}".format(domain_col, domain))
            col_ids = list(domain_col.keys())
            col_ids.sort()
            text_data = b""
            for obj_id in col_ids:
                col_obj = domain_col[obj_id]
                line = "{} {} {} {}\n".format(obj_id, col_obj["ETag"], col_obj["LastModified"], col_obj["Size"])
                line = line.encode('utf8')
                text_data += line
                if getCollectionForId(obj_id) == "datasets":
                    # create chunk listing
                    if objs_updated is None:
                        await updateDatasetContents(app, domain, obj_id)
                    else:
                        # if objs_updated is passed in, only update if at least one chunk in
                        # the dataset has been updated
                        for updated_id in objs_updated:
                            if isValidChunkId(updated_id) and getDatasetId(updated_id) == obj_id:
                                await updateDatasetContents(app, domain, obj_id)
                                break

            log.info("write collection key: {}, count: {}".format(col_s3key, len(col_ids)))
            try:
                await putS3Bytes(app, col_s3key, text_data)
            except HttpProcessingError:
                log.error("S3 Error writing {}.json key: {}".format(collection, col_s3key))

    log.info("updateDomainContent: {} Done".format(domain))

async def sweepObj(app, objid, force=False):
    """ Delete the given object if it has been created more the x seconds ago.
    Log S3 errors, but don't raise an exception. """
    s3keys = app["s3keys"]
    log.info("sweepObj {}".format(objid))
    s3key = getS3Key(objid)
    if s3key not in s3keys:
        log.error("sweepObj, key: {} not found".format(s3key))
        return False
    obj = s3keys[s3key]
    if "LastModified" not in obj:
        log.error("Expected LastModified in s3key dict")
        return False
    lastModified = obj["LastModified"]
    now = time.time()
    if not force and now - lastModified < app["anonymous_ttl"]:
        log.info("obj: {} isn't old enough to delete yet".format(objid))
        return False

    if "Size" not in obj:
        log.error("Expected Size is s3key dict")
        return False
    num_bytes = obj["Size"]

    req = getDataNodeUrl(app, objid)
    collection = "chunks"
    if not isValidChunkId(objid):
        collection = getCollectionForId(objid)

    req += '/' + collection + '/' + objid
    log.info("Delete object {}, [{} bytes]".format(objid, num_bytes))
    params = {"Notify": 0}  # Let the DN not to notify the AN node about this action
    try:
        await http_delete(app, req, params=params)
        success = True
    except HttpProcessingError as hpe:
        log.warn("Error deleting obj {}: {}".format(objid, hpe.code))
        success = False
        # TBD: add back to s3keys?
    if success:
        objids = app[collection]
        if objid not in objids:
            log.warn("expected to find {} in collection: {}".format(objid, collection))
        else:
            del objids[objid]
        if s3key not in s3keys:
            log.warn("expected to find key: {} in s3keys".format(s3key))
        else:
            del s3keys[s3key]
        app["bytes_in_bucket"] -= num_bytes
    log.info("sweepObj {} done".format(objid))    
    return success

async def sweepObjs(app):
    """ Iterate through the object tree and delete any unlinked objects """
    #s3keys = app["s3keys"]
    groups = app["groups"]
    datasets = app["datasets"]
    datatypes = app["datatypes"]
    bucket_stats = app["bucket_stats"]
    if "deleted_count" not in bucket_stats:
        bucket_stats["deleted_count"] = 0
    log.info("sweepObjs")
    deleted_count = bucket_stats["deleted_count"]

    # datasets
    deleted_ids = []
    for dsetid in datasets:
        dset_obj = datasets[dsetid]
        if not dset_obj["used"]:
            deleted_ids.append(dsetid)
    
    for dsetid in deleted_ids:    
        dset_obj = datasets[dsetid]
        if await sweepObj(app, dsetid):  
            deleted_count += 1

            # delete any chunks
            if "chunks" not in dset_obj:
                log.warn("expected chunks key in dataset: {}".format(dsetid))
                continue
            
            deleted_chunk_ids = []
            dset_chunks = dset_obj["chunks"]
            for chunkid in dset_chunks:
                if await sweepObj(app, chunkid):
                    deleted_chunk_ids.append(chunkid)
            for chunkid in deleted_chunk_ids:
                del dset_chunks[chunkid]
            deleted_count += len(deleted_chunk_ids)
    
    # datatypes
    deleted_ids = []
    for datatypeid in datatypes:
        datatype_obj = datatypes[datatypeid]
        if not datatype_obj["used"]:
            deleted_ids.append(datatypeid)

    for datatypeid in deleted_ids:
        if await sweepObj(app, datatypeid):
            deleted_count += 1

    # groups
    deleted_ids = []
    for groupid in groups:
        group_obj = groups[groupid]
        if not group_obj["used"]:
            deleted_ids.append(groupid)
    for groupid in deleted_ids:
        if await sweepObj(app, groupid):
            deleted_count += 1
    
    bucket_stats["deleted_count"] += deleted_count
    # iteratate through 
    log.info("SweepObjs done")

#
# pending queue handler
#
async def domainDelete(app, domain):
    """ Process domain deletion event """
    log.info("domainDelete: {}".format(domain))
    domains = app["domains"]
    if domain not in domains:
        log.warn("Expected to find domain: {} in collection".format(domain))
        return
    s3keys = app["s3keys"]
    domain_key = getS3Key(domain)
    num_bytes = 0
    if domain_key not in s3keys:
        log.warn("expected to find domain key: {} in s3keys".format(domain_key))
    else:
        domain_obj = s3keys[domain_key]
        del s3keys[domain_key]
        num_bytes = domain_obj["Size"]
    app["bytes_in_bucket"] -= num_bytes

    del domains[domain]
    await asyncio.sleep(0)

async def rootDelete(app, rootid):
    """ get root obj for rootid """
    roots = app["roots"]
    if rootid not in roots:
        log.warn("expected to find: {} in roots collection".format(rootid))

    root_obj = roots[rootid]
    root_groups = root_obj["groups"]
    for grpid in root_groups:
        await sweepObj(app, grpid, force=True)
    # delete all types of domain
    root_datatypes = root_obj["datatypes"]
    for datatypeid in root_datatypes:
        await sweepObj(app, datatypeid, force=True)
    # delete all datasets of domain
    root_datasets = root_obj["datasets"]
    for dsetid in root_datasets:
        dataset_obj = root_datasets[dsetid]
        # for each dataset, delete all its chunks
        dataset_chunks = dataset_obj["chunks"]
        for chunkid in dataset_chunks:
            await sweepObj(app, chunkid, force=True)
        await sweepObj(app, dsetid, force=True)
    

async def domainCreate(app, domain):
    """ Process domain creation event """
    log.info("domainCreate: {}".format(domain))
    domain_key = getS3Key(domain)
    s3keys = app["s3keys"]
    domains = app["domains"]
    if domain_key in s3keys:
        log.warn("domain key: {} not expected in s3keys".format(domain_key))
        return
    if domain in domains:
        log.warn("domain: {} not expected in domains".format(domain))
        return

    domain_obj = await getS3ObjStats(app, domain_key) # ETag, LastModified, Size
    domain_json = await getS3JSONObj(app, domain_key)

    if "root" in domain_json:
        rootid = domain_json["root"]
    else:
        rootid = None
    if rootid:
        domain_obj["root"] = rootid
    num_bytes = domain_obj["Size"]
    app["bytes_in_bucket"] += num_bytes
    s3keys[domain_key] = domain_obj
    domains[domain] = domain_obj

    

async def getDomainForObjid(app, objid):
    """ get domain for the object """
    if isValidChunkId(objid):
        domain_item_id = getDatasetId(objid)
    else:
        domain_item_id = objid  # groups/datatypes/datsaets will have domain key
    try:
        obj_json = await getS3JSONObj(app, getS3Key(domain_item_id))
    except HttpProcessingError as hpe:
        log.warn("got {} fetching obj: {}".format(hpe.code, domain_item_id))
        return None
    if "domain" not in obj_json:
        log.warn("expected to find domain key in dataset: {}".format(objid))
        return None
    domain = obj_json["domain"]
    log.info("Got domain: {} for objid: {}".format(domain, objid))
    return domain

async def getRootForObjid(app, objid):
    """ get root id for the object """
    if isValidChunkId(objid):
        domain_item_id = getDatasetId(objid)
    else:
        domain_item_id = objid  # groups/datatypes/datsaets will have domain key
    try:
        obj_json = await getS3JSONObj(app, getS3Key(domain_item_id))
    except HttpProcessingError as hpe:
        log.warn("got {} fetching obj: {}".format(hpe.code, domain_item_id))
        return None
    if "root" not in obj_json:
        log.warn("expected to find domain key in dataset: {}".format(objid))
        return None
    rootid = obj_json["rootid"]
    log.info("Got root: {} for objid: {}".format(rootid, objid))
    return rootid

async def getDomainCollectionForObjId(app, objid):
    """ Return the domain collection for the given objid """
    rootid = await getRootForObjid(app, objid)
    if rootid is None:
        log.warn("couldn't get domain for objid: {}".format(objid))
        return

    roots = app["roots"]
    if rootid in roots:
        root_obj = roots[rootid]
    else:
        s3key = getS3Key(rootid)
        s3keys = app["s3keys"]
        if s3key not in s3keys:
            # object not yet loaded?
            root_obj = { }
            s3keys[s3key] = root_obj
        else:
            root_obj = s3keys[s3key]
        if "datasets" not in root_obj:
            root_obj["datasets"] = {}
        if "datatypes" not in root_obj:
            root_obj["datatypes"] = {}
        if "groups" not in root_obj:
            root_obj["groups"] = {}
        
        roots[rootid] = root_obj

    if isValidChunkId(objid):
        # chunks are members of their dataset
        if "datasets" not in root_obj:
            log.warn("expected to find datasets collection in root obj :{}".format(rootid))
            return  
        domain_datasets = root_obj["datasets"]
        dsetid = getDatasetId(objid)  # dataset id for this chunk
        if dsetid not in domain_datasets:
            log.warn("expected to find dataset: {} in domain collection for: {}".format(dsetid, rootid))
            s3key = getS3Key(dsetid) 
            s3keys = app["s3keys"]
            if s3key not in s3keys:
                dset_obj = {}
                s3keys[s3key] = dset_obj
            else:
                dset_obj = s3keys[s3key]
            domain_datasets[dsetid] = dset_obj
        else:
            dset_obj = domain_datasets[dsetid]
        if "chunks" not in dset_obj:
            dset_obj["chunks"] = {}
 
        domain_col = dset_obj["chunks"]
    else:
        # dataset/group/datatype obj  
        collection = getCollectionForId(objid)     
        if collection not in root_obj:
            log.warn("expected to find {} collection in domain obj :{}".format(collection, rootid))
            return  
        domain_col = root_obj[collection]
    return domain_col

async def objUpdate(app, objid):
    """ Process object update event """
    log.info("objUpdate: {}".format(objid))

    if isValidChunkId(objid):
        collection = "chunks"
    elif isValidUuid(objid):
        collection = getCollectionForId(objid)
    else:
        log.error("Got unexpected objid: {}".format(objid))
        return

    s3key = getS3Key(objid)
    try:
        s3stats = await getS3ObjStats(app, s3key)
    except HttpProcessingError as hpe:
        log.warn("objUpdate - getS3ObjStats error: {} for key: {}".format(hpe.code, s3key))
        return
    s3keys = app["s3keys"]
    old_size = 0
    if s3key in s3keys:
        # this is a replace
        old_stats = s3keys[s3key]
        old_size = old_stats["Size"]
        # copy any ancillary keys to the new obj
        for k in old_stats:
            if k not in ("Size", "ETag", "LastModified"):
                s3stats = old_stats[k]
    # add any expected keys not already present
    if collection == "datasets" and "chunks" not in s3stats:
        s3stats["chunks"] = {}
    if "used" not in s3stats:
        s3stats["used"] = False

    s3keys[s3key] = s3stats

    # adjust the total size of the bucket
    app["bytes_in_bucket"] -= old_size
    app["bytes_in_bucket"] += s3stats["Size"]

    # add/replace from the global collection
    global_collection = app[collection]
    global_collection[objid] = s3stats  # may be replace or insert

    # get domain collection for the object
    domain_col = await getDomainCollectionForObjId(app, objid)
    if domain_col is None:
        log.warn("couldn't get domain collection for update objid: {}".format(objid))
        return
 
    domain_col[objid] = s3stats  # insert/replace
         

async def objDelete(app, objid):
    """ Process object delete event """
    log.info("objectDelete: {}".format(objid))

    if isValidChunkId(objid):
        collection = "chunks"
    elif isValidUuid(objid):
        collection = getCollectionForId(objid)
    else:
        log.error("Got unexpected objid: {}".format(objid))
        return
    deleted_objids = app["deleted_objids"]
    deleted_objids.add(objid)  
    s3key = getS3Key(objid) 
    s3keys = app["s3keys"]
    if s3key not in s3keys:
        log.warn("expected to find objid in s3keys: {}".format(objid))
        return
    s3stats = s3keys[s3key]
    del s3keys[s3key]  # remove from s3key collection
    
    
    # adjust the total size of the bucket
    app["bytes_in_bucket"] -= s3stats["Size"]

    # delete from the global collection
    global_collection = app[collection]
    del global_collection[objid]  

    # get domain collection for the object
    domain_col = await getDomainCollectionForObjId(app, objid)
    if domain_col is None:
        log.warn("couldn't get domain collection for objid: {}".format(objid))
        return

    del domain_col[objid]  # remove from the collection


async def bucketCheck(app):
    """ Periodic method that iterates through all keys in the bucket  
    """

    #initialize these objecs here rather than in main to avoid "ouside of coroutine" errors

    app["last_bucket_check"] = int(time.time())

    # update/initialize root object before starting node updates
 
    while True:  
        if app["node_state"] != "READY":
            log.info("bucketCheck waiting for Node state to be READY")
            await asyncio.sleep(1)
        else:
            break

    now = int(time.time())
    log.info("bucket check {}".format(unixTimeToUTC(now)))
    # do initial listKeys
    await listKeys(app)
     
    log.info("Mark domain objects")
    domains = app["domains"]
    # check each domain
    for domain in domains:
        # mark objects that are linked
        await markObj(app, domain, updateLinks=True)
    log.info("Mark donain objects done")
    # remove any unlinked objects
    log.info("sweepObjs start")
    await sweepObjs(app)
    log.info("sweepObjs done")
    log.info("updateBucketStats")
    updateBucketStats(app)

    # do GC for all domains at startup
    log.info("updateDomainContent start")
    for domain in domains:
        log.info("domain: {}".format(domain))
        # organize collections of groups/datasets/and datatypes for each domain

        try:
            await updateDomainContent(app, domain)
        except Exception  as e:
            log.warn("got exception in updateDomainContent for domain: {}: {}".format(domain, e))
            continue 
    log.info("updateDomainContent done")

    while True:
        # sleep for a bit
        sleep_secs = config.get("async_sleep_time")
        log.info("Bucket check sleeping for {}".format(sleep_secs))
        await  asyncio.sleep(sleep_secs)
        now = int(time.time())
        log.info("bucket check {}".format(unixTimeToUTC(now)))
        pending_queue = app["pending_queue"]
        domains_updated = {}
        while len(pending_queue) > 0:
            item = pending_queue.pop(0)  # remove from the front
            objid = item["objid"]
            action = item["action"]
            log.info("pop from pending queue: obj: {} action: {}".format(objid, action))
            
            if isValidDomain(objid):
                if action == "DELETE":
                    await domainDelete(app, objid)
                elif action == "PUT":
                    await domainCreate(app, objid)
                else:
                    log.error("Unexpected action: {}".format(action))
            elif isValidChunkId(objid) or isValidUuid(objid):
                if action == "PUT":
                    await objUpdate(app, objid)
                elif action == "DELETE":
                    await objDelete(app, objid)
                else:
                    log.error("Unexpected action: {}".format(action))
                    continue
                domain = await getDomainForObjid(app, objid)
                log.info("domain: {}".format(domain))
                
                if domains is None:
                    log.warn("coudn't find domain for objid: {}".format(objid))
                elif domain in domains_updated:
                    objids = domains_updated[domain]
                    objids.add(objid)
                else:
                    objids = set()
                    objids.add(objid)
                    domains_updated[domain] = objids
            else:
                log.error("Unexpected objid: {}".format(objid))
        log.info("finished processing pending queue")
        # For each updated domain, do a mark and sweep
        log.info("{} domains will be updated".format(len(domains_updated)))
        for domain in domains_updated:
            objs_updated = domains_updated[domain]
            log.info("updateDomainContent - domain: {} objs_updated: {}".format(domain, objs_updated))
            #await updateDomainContent(app, domain, objs_updated=objs_updated)
        log.info("finished updating domain contents")


def updateBucketStats(app):  
    """ Collect some high level stats for use by the info request """
    bucket_stats = app["bucket_stats"]
    if "s3keys" in app:
        s3keys = app["s3keys"]
        bucket_stats["object_count"] = len(s3keys)
    if "domains" in app:
        domains = app["domains"]
        bucket_stats["domain_count"] = len(domains)
    if "root" in app:
        roots = app["roots"]
        bucket_stats["root_count"] = len(roots)
    if "groups" in app:
        groups = app["groups"]
        bucket_stats["group_count"] = len(groups)
    if "datasets" in app:
        datasets = app["datasets"]
        bucket_stats["dataset_count"] = len(datasets)
    if "datatypes" in app:
        datatypes = app["datatypes"]
        bucket_stats["datatype_count"] = len(datatypes)
    if "chunks" in app:
        chunks = app["chunks"]
        bucket_stats["chunk_count"] = len(chunks)
    if "bytes_in_bucket" in app:
        bucket_stats["storage_size"] = app["bytes_in_bucket"]
    if "pending_queue" in app:
        pending_queue = app["pending_queue"]
        bucket_stats["pending_count"] = len(pending_queue) 

async def GET_AsyncInfo(request):
    """HTTP Method to retun async node state to caller"""
    log.request(request)
    app = request.app
    resp = StreamResponse()
    resp.headers['Content-Type'] = 'application/json'
    updateBucketStats(app)
    answer = {}
    answer["bucket_stats"] = app["bucket_stats"]
    resp = await jsonResponse(request, answer) 
    log.response(request, resp=resp)
    return resp

async def PUT_Objects(request):
    """HTTP method to notify creation/update of objid"""
    log.request(request)
    app = request.app
    log.info("PUT_Objects")
    if not request.has_body:
        msg = "PUT objects with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    body = await request.json()
    if "objids" not in body:
        msg = "expected to find objids key in body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    objids = body["objids"]
    for objid in objids:
        if not isValidDomain(objid) and not isValidUuid(objid):
            msg = "PUT_Objects Invalid id: {}".format(objid)
            log.warn(msg)
            raise HttpBadRequest(message=msg)

    pending_queue = app["pending_queue"]
    for objid in objids:
        if isValidDomain(objid):
            # ignore domain events
            continue
        item = {"objid": objid, "action": "PUT"}
        log.info("adding item: {} to pending queue".format(item))
        pending_queue.append(item)

    resp_json = {  } 
    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp

async def DELETE_Objects(request):
    """HTTP method to notify deletion of objid"""
    log.request(request)
    app = request.app
    log.info("DELETE_Objects")

    if not request.has_body:
        msg = "PUT objects with no body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)

    body = await request.json()
    if "objids" not in body:
        msg = "expected to find objids key in body"
        log.warn(msg)
        raise HttpBadRequest(message=msg)
    objids = body["objids"]
    for objid in objids:
        if not isValidDomain(objid) and not isValidUuid(objid):
            msg = "DELETE_Objects Invalid id: {}".format(objid)
            log.warn(msg)
            raise HttpBadRequest(message=msg)

    pending_queue = app["pending_queue"]
    for objid in objids:
        if isValidDomain(objid):
            # ignore domain events
            continue
        item = {"objid": objid, "action": "DELETE"}
        log.info("adding item: {} to pending queue".format(item))
        pending_queue.append(item)

    resp_json = {  } 
    resp = await jsonResponse(request, resp_json)
    log.response(request, resp=resp)
    return resp


async def init(loop):
    """Intitialize application and return app object"""
    
    app = baseInit(loop, 'an')
    app.router.add_route('GET', '/async_info', GET_AsyncInfo)
    app.router.add_route('PUT', '/objects', PUT_Objects)
    app.router.add_route('DELETE', '/objects', DELETE_Objects)
    app["bucket_stats"] = {}
    # object and domain updates will be posted here to be worked on offline
    app["pending_queue"] = [] 
    app["deleted_objids"] = set()
     
    return app

#
# Main
#

if __name__ == '__main__':
    log.info("AsyncNode initializing")
    
    loop = asyncio.get_event_loop()
    app = loop.run_until_complete(init(loop))   
    # run background tasks
    asyncio.ensure_future(bucketCheck(app), loop=loop)
    asyncio.ensure_future(healthCheck(app), loop=loop)
    async_port = config.get("an_port")
    app["anonymous_ttl"] = config.get("anonymous_ttl")
    app["s3_sync_interval"] = config.get("s3_sync_interval")
    log.info("Starting service on port: {}".format(async_port))
    run_app(app, port=int(async_port))