
# coding: utf-8


from __future__ import division

#Config Camera
RESOLUTION = (640, 480)
FPS = 30
ROTATION = 180
MD_BLOCK_FRACTION = 0.008 #Fraction of blocks that must show movement
MD_SPEED = 2.0            #How many screens those blocks must move per second
MD_FALLOFF = 0.5          #How many seconds no motion must be present to trigger completion of a scene

#Config Persistency
DATA_FOLDER = './data'

#Config Azure
AZURE_COG_HOST = 'https://westus.api.cognitive.microsoft.com/vision/v1.0/analyze'
AZURE_COG_RETRIES = 3
from creds.credentials import *
from device.D2CMsgSender import D2CMsgSender

import numpy as np
import PIL.Image
import picamera
import picamera.array

from queue import Queue
from collections import namedtuple  #Forgo typing to maintain vanilla python 3.4 compatibility on RPi

import json
import io, os
import socket, requests
import time, datetime

#Schema
Motion = namedtuple('Motion', 'timestamp, triggered, vectors_x, vectors_y, sad, magnitude')
Snapshot = namedtuple('Snapshot','timestamp, img_rgb, motion')
PictureEvent = namedtuple('PictureEvent', 'timestamp, type, on, data')
SceneCapture = namedtuple('SceneCapture', 'pic_on, pic_off')



def to_jpg(rgb):
    f = io.BytesIO()
    PIL.Image.fromarray(rgb).save(f, 'jpeg')
    return f.getvalue()
    
def to_ISO(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).isoformat()

def to_ID(timestamp, on):
    return str(to_ISO(timestamp).replace(':', '_')) + ('_on' if on else '_off')

def get_convert_jpg(pic: PictureEvent, modify=True):
    jpg = pic.data
    if pic.type == 'rgb':
        jpg = to_jpg(pic.data)
        if modify: 
            pic.data = jpg
            pic.type = 'jpg'
    #Todo: Exception handling
    return jpg

def save_jpg(jpg, _id):
    if DATA_FOLDER is not None:
        with open(os.path.join(DATA_FOLDER, _id+'.jpg'), "wb") as f:   
            f.write(jpg)
        



#Derive more constants
BLOCKSIZE = 16
MOTION_W = RESOLUTION[0] // BLOCKSIZE + 1
MOTION_H = RESOLUTION[1] // BLOCKSIZE + 1
BLOCKS = (MOTION_W)*(MOTION_H)
MD_BLOCKS = int(MD_BLOCK_FRACTION * BLOCKS)
MD_MAGNITUDE = int(MD_SPEED / FPS * RESOLUTION[0])
print("MD if >%i out of %i blocks show >%i pixel movement in a %i wide frame" % (MD_BLOCKS, BLOCKS, MD_MAGNITUDE, RESOLUTION[0]))



scene_queue = Queue(3)          #Queue three full scenes
motion_queue = Queue(FPS * 10)  #Queue a maxmimum of 10 seconds motion-data
picture_queue = Queue(4)        #Queue a maximum pair of two on and off snapshots

#Shared state for image and video analyzers
#Todo: Encapsulate state better by passing into constructor or multiple inheritance from both detectors 
current_state = {
    'motion_vectors_raw' : None,
    'motion_magnitude_raw': None,
    'last_md_time_true' : None,
    'last_md_time_false' : time.time(),
    'md': False,
    'rgb' : None,
    'last_pic_on' : None
}

class MyRGBAnalysis(picamera.array.PiRGBAnalysis):
    def analyse(self, a): 
        current_state['rgb'] = a 


class MyMotionDetector(picamera.array.PiMotionAnalysis):
    def analyse(self, a):
        m = np.sqrt(
            np.square(a['x'].astype(np.float)) +
            np.square(a['y'].astype(np.float))
            ).clip(0, 255).astype(np.uint8)

        current_state['motion_vectors_raw'] = a
        current_state['motion_magnitude_raw'] = m

        
        # If there're more than MD_BLOCKS vectors with a magnitude greater
        # than MD_MAGNITUDE, then say we've detected motion
        #Todo: does motion- or RGB analysis come first? In the former case current_state['rgb'] lags one frame
        md = ((m > MD_MAGNITUDE).sum() > MD_BLOCKS)
        
        now = time.time()
        motion = Motion(now, md, a['x'], a['y'], a['sad'], m)            
        snap = Snapshot(now, current_state['rgb'], motion)
        
        md_update(snap)
        
def md_update(snap: Snapshot):
    now = snap.timestamp
    before = current_state['last_md_time_true']
    is_motion = snap.motion.triggered
    
    md = current_state['md']
    
    #Test if motion detection flipped over
    if is_motion:
        current_state['last_md_time_true'] = now
        if not md:
            md_rising(snap)
            current_state['md'] = True
    else:
        current_state['last_md_time_false'] = now
        if md is True and before is not None and (now - before) > MD_FALLOFF:
            md_falling(snap)
            current_state['md'] = False 
    
    #Queue motion data
    if current_state['md']:
        motion_queue.put(snap.motion)
        
            
#Attention: runs synchronous to motion detection
def md_rising(snap: Snapshot):
    now = snap.timestamp    
    motion = snap.motion
    
    #Calculate Summary statistics only for debugging purposes
    avg_x = motion.vectors_x.sum() / RESOLUTION[0]
    avg_y = motion.vectors_y.sum() / RESOLUTION[1]
    avg_m = motion.magnitude.sum() / (RESOLUTION[0] * RESOLUTION[1])
    
    print('Motion detected, avg_x: %i, avg_y: %i, mag: %i' % (avg_x, avg_y, avg_m) )
    
    pic = PictureEvent(now, 'rgb', True, snap.img_rgb)
    current_state['last_pic_on'] = pic
    picture_queue.put(pic)
    
def md_falling(snap):
    now = snap.timestamp
    print("Motion vanished after %f secs" % (now - current_state['last_md_time_true']))
    
    pic = PictureEvent(now, 'jpg', False, to_jpg(snap.img_rgb))
    picture_queue.put(pic)
    scene_queue.put(SceneCapture(current_state['last_pic_on'], pic))



def processRequest( json, data, headers, params ):
    #From example code of project oxford 
    """
    Parameters:
    json: Used when processing images from its URL. See API Documentation
    data: Used when processing image read from disk. See API Documentation
    headers: Used to pass the key information and the data type request
    """
    retries = 0
    result = None

    while True:
        response = requests.request( 'post', AZURE_COG_HOST, json = json, data = data, headers = headers, params = params )
        if response.status_code == 429: 
            print( "Message: %s" % ( response.json()['error']['message'] ) )
            if retries <= AZURE_COG_RETRIES: 
                time.sleep(1) 
                retries += 1
                continue
            else: 
                print( 'Error: failed after retrying!' )
                break

        elif response.status_code == 200 or response.status_code == 201:
            if 'content-length' in response.headers and int(response.headers['content-length']) == 0: 
                result = None 
            elif 'content-type' in response.headers and isinstance(response.headers['content-type'], str): 
                if 'application/json' in response.headers['content-type'].lower(): 
                    result = response.json() if response.content else None 
                elif 'image' in response.headers['content-type'].lower(): 
                    result = response.content
        else:
            print( "Error code: %d" % ( response.status_code ) )
            print( "Message: %s" % ( response.json()['error']['message'] ) )
        break
        
    return result

def analyze_pic(jpg, features='Color,Categories,Tags,Description'):
    params = { 'visualFeatures' : features} 
    headers = dict()
    headers['Ocp-Apim-Subscription-Key'] = AZURE_COG_KEY
    headers['Content-Type'] = 'application/octet-stream'
    result = processRequest(None, jpg, headers, params )
    return result



#Custom encoder for objects containing numpy 
class MsgEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(MsgEncoder, self).default(obj)

#Normalized versions with summary stats that can be sent to the cloud
MotionEvent = namedtuple('MotionEvent', 'timestamp, triggered, blocks_x, blocks_y, vectors_x, vectors_y, avg_x, avg_y, mag, sad')
SceneEvent = namedtuple('SceneEvent', 'timestamp_on, timestamp_off, caption, caption_conf, tags')        


last_description = ''
def dispatch_scene(azure_msg):
    while True:
        scene = scene_queue.get()
        
        jpg_off = get_convert_jpg(scene.pic_off, False)
        
        result = analyze_pic(jpg_off)
        caption = result['description']['captions'][0]['text']
        caption_confidence = result['description']['captions'][0]['confidence']
        tags = result['description']['tags']
        on = to_ISO(scene.pic_on.timestamp)
        off = to_ISO(scene.pic_off.timestamp)

        event = SceneEvent(on, off, caption, caption_confidence, tags)
        print(event)
        azure_msg.sendD2CMsg(AZURE_DEV_ID, json.dumps(event._asdict(), cls=MsgEncoder))
        
        scene_queue.task_done()
                
def dispatch_motiondata(azure_msg):
    #SnapshotEvents
    #SetOption Batching to False to come closer to realtime HTTP calls.
    #Caveat: If MotionEvent queue is not emptied yet, it will block this messages. Todo: run in separate thread.
    #Discussion here: https://github.com/Azure/azure-iot-sdk-python/issues/15
    
    #MotionEvents
    #SetOption Batching to True to save HTTP calls    
    while True:
        m = motion_queue.get()

        #MotionEvent = namedtuple('MotionEvent', 'timestamp, triggered, blocks_x, blocks_y, vectors_x, vectors_y, avg_x, avg_y, min_x, min_y, mag')
        avg_x = m.vectors_x.sum() / RESOLUTION[0]
        avg_y = m.vectors_y.sum() / RESOLUTION[1]
        avg_m = m.magnitude.sum() / (RESOLUTION[0] * RESOLUTION[1])
        
        me = MotionEvent(to_ISO(m.timestamp), to_ISO(m.triggered), MOTION_W, MOTION_H, list(m.vectors_x.flatten()), list(m.vectors_y.flatten()),                          avg_x, avg_y, avg_m, list(m.sad.flatten()))

        #print(nr_motion)
        #azure_msg.sendD2CMsg(AZURE_DEV_ID, json.dumps(me._asdict(), cls=MsgEncoder))
        motion_queue.task_done()
        
last_time_on = None
last_time_off = None
id_on = None
id_off = None

def publish_pictures():
    while True:
        p = picture_queue.get()

        jpg = get_convert_jpg(p, False)
        _id = to_ID(p.timestamp, p.on)
        save_jpg(jpg, _id)
        
        picture_queue.task_done()  



import _thread

azure_msg = D2CMsgSender(AZURE_DEV_CONNECTION_STRING)


with picamera.PiCamera() as camera:      
    camera.resolution = RESOLUTION
    camera.framerate = FPS
    camera.rotation = ROTATION
    
    #Set up motion and video stream analyzer
    camera.start_recording(
        '/dev/null',
        format='h264',
        motion_output=MyMotionDetector(camera)
        )
    #Set up RGB capture in parallel
    camera.start_recording(
        MyRGBAnalysis(camera),
        format='rgb',
        splitter_port=2
    )
    camera.wait_recording(0.5)

    
    _thread.start_new_thread(dispatch_scene, (azure_msg,))
    _thread.start_new_thread(dispatch_motiondata, (azure_msg,))
    _thread.start_new_thread(publish_pictures, ())

    while True:       
        try:
            time.sleep(1)       
        except KeyboardInterrupt:
            break

    camera.stop_recording(splitter_port=2)
    camera.stop_recording()
    
    scene_queue.join()
    motion_queue.join()
    picture_queue.join()





