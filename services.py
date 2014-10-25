import json
import os,re
import logging
import urllib
import itertools
from operator import itemgetter
from datetime import datetime

import webapp2
from google.appengine.ext.webapp import blobstore_handlers
from google.appengine.ext import blobstore, deferred
from google.appengine.api import files, images
from google.appengine.api import search

from models import Stream, User, Image

REPORT_RATE_MINS      = "0"
REPORT_LAST           = None

MIN_FILE_SIZE         = 1  # bytes
MAX_FILE_SIZE         = 5000000  # bytes
IMAGE_TYPES           = re.compile('image/(gif|p?jpeg|(x-)?png)')
ACCEPT_FILE_TYPES     = IMAGE_TYPES
THUMBNAIL_MODIFICATOR = '=s80'  # max width / height
EXPIRATION_TIME       = 300  # seconds

INDEX_NAME            = "IDX"


def cleanup(blob_keys):
    blobstore.delete(blob_keys)


class ServiceHandler(webapp2.RequestHandler):
  def respond(self,**response):
    if 'error' in response and response['error']:
      logging.error("Services: "+response['error'])
    elif 'status' in response:
      logging.debug("Services: "+response['status'])

    return self.response.write(json.dumps(response))


class ServeHandler(blobstore_handlers.BlobstoreDownloadHandler):
  def get(self):
    blob_key = self.request.get('blob_key')
    if not blobstore.get(blob_key):
      self.error(404)
    else:
      self.send_blob(blob_key)


class UploadHandler(blobstore_handlers.BlobstoreUploadHandler):
  def initialize(self, request, response):
    super(UploadHandler, self).initialize(request, response)
    self.response.headers['Access-Control-Allow-Origin'] = '*'
    self.response.headers[
      'Access-Control-Allow-Methods'
    ] = 'OPTIONS, HEAD, GET, POST, PUT, DELETE'
    self.response.headers[
      'Access-Control-Allow-Headers'
    ] = 'Content-Type, Content-Range, Content-Disposition'

  def validate(self, file):
    if file['size'] < MIN_FILE_SIZE:
        file['error'] = 'File is too small'
    elif file['size'] > MAX_FILE_SIZE:
        file['error'] = 'File is too big'
    # elif not ACCEPT_FILE_TYPES.match(file['type']):
    #     file['error'] = 'Filetype not allowed'
    else:
        return True
    return False

  def get_file_size(self, file):
    file.seek(0, 2)  # Seek to the end of the file
    size = file.tell()  # Get the position of EOF
    file.seek(0)  # Reset the file position to the beginning
    return size

  def write_blob(self, data, info):
    blob = files.blobstore.create(mime_type=info['type'],
                                  _blobinfo_uploaded_filename=info['name'])
    with files.open(blob, 'a') as f:
        f.write(data)
    files.finalize(blob)

    print blob

    user_photo = Image(stream_id=self.request.get('stream_id')
                       ,image_id=self.request.get('image_id')
                       ,blob_key=files.blobstore.get_blob_key(blob))

    stream = Stream.getStream(self.request.get('stream_id'))
    if stream:
      logging.debug("Adding image at %s (%r)" % (user_photo.create_date,str(user_photo.blob_key)))
      stream.addImage(user_photo)

    return files.blobstore.get_blob_key(blob)

  def post(self):
    result = {'files': self.handle_upload()}

    # redirect = self.request.get('redirect')
    # if redirect:
    #     return self.redirect(redirect)
    # self.redirect('/viewstream?'+urllib.urlencode({'stream_id':self.request.get('stream_id')
    #                                                ,'user_id':self.request.get('user_id')}))
    if 'application/json' in self.request.headers.get('Accept'):
        self.response.headers['Content-Type'] = 'application/json'

    self.response.write(json.dumps(result, separators=(',', ':')))


  def handle_upload(self):
    results = []
    blob_keys = []
    for name, fieldStorage in self.request.POST.items():
        if type(fieldStorage) is unicode:
            continue
        result = {}
        result['name'] = re.sub(r'^.*\\','',fieldStorage.filename)
        result['type'] = fieldStorage.type
        result['size'] = self.get_file_size(fieldStorage.file)
        if self.validate(result):
            blob_key = str(self.write_blob(fieldStorage.value, result))
            blob_keys.append(blob_key)
            result['deleteType'] = 'DELETE'
            result['deleteUrl'] = self.request.host_url +\
                '/?key=' + urllib.quote(blob_key, '')
            if (IMAGE_TYPES.match(result['type'])):
                try:
                    result['url'] = images.get_serving_url(
                        blob_key,
                        secure_url=self.request.host_url.startswith(
                            'https'
                        )
                    )
                    result['thumbnailUrl'] = result['url'] +\
                        THUMBNAIL_MODIFICATOR
                except:  # Could not get an image serving url
                    pass
            if not 'url' in result:
                result['url'] = self.request.host_url +\
                    '/' + blob_key + '/' + urllib.quote(
                        result['name'].encode('utf-8'), '')
        results.append(result)
    deferred.defer(cleanup,blob_keys,_countdown=EXPIRATION_TIME)
    return results

  def delete(self):
    key = self.request.get('key') or ''
    if key:
      img = Image.getImage(key)
      Stream.getStream(img.stream_id).removeImage(img.image_id)
    blobstore.delete(key)
    s = json.dumps({key: True}, separators=(',', ':'))
    if 'application/json' in self.request.headers.get('Accept'):
      self.response.headers['Content-Type'] = 'application/json'
    self.response.write(s)
    

class SubscribeStreamsHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)

        user = User.getUser(form['user_id'])
        if not user:
          return self.respond(error="User %(user_id)r does not exist" % form)

        for stream_id in form['streams']:
            user.subscribeStream(stream_id)
        self.respond(status="Subscribed user %(user_id)r to %(streams)s" % form)


class UnsubscribeStreamsHandler(ServiceHandler):
    def post(self):
      form = json.loads(self.request.body)

      user = User.getUser(form['user_id'])
      if not user:
        return self.respond(error="User %(user_id)r does not exist" % form)

      for stream_id in form['streams']:
        user.unsubscribeStream(stream_id)

      self.respond(status="Unsubscribed user %(user_id)r from %(streams)s" % form)


class AddViewHandler(ServiceHandler):
  def post(self):
    form = json.loads(self.request.body)          

    stream = Stream.getStream(form['stream_id'])
    if not stream:
        return self.respond(error="Stream %(stream_id)r does not exist." % form)

    stream.addView()
    self.respond(status="Added new view to stream %r at %s." % (form['stream_id']
                                                                ,stream.views[0]))


class SendReportHandler(ServiceHandler):
  def post(self):
    if REPORT_RATE_MINS == "0": return
    if not REPORT_LAST:
      REPORT_LAST = datetime.now()
      return
    td = (datetime.now() - REPORT_LAST).seconds
    if td < int(REPORT_RATE_MINS)*60: return
    REPORT_LAST = datetime.now()

    from google.appengine.api import mail as gmail

    form = [str(datetime.now())]
    streams = sorted(Stream.dump(),key=lambda x: len(x.views),reverse=True)[:3]
    streams = map(itemgetter('stream_id','views'),streams)
    form.extend(streams)

    message = gmail.EmailMessage()
    message.sender = "toemossgarcia@gmail.com"
    message.to = "tjgarcia@utexas.edu"
    message.subject = "[Connex.us] Trending report for %s." % form[0]
    message.body = """\
    Here are the Top 3 streams for {0:}

    1. {1[0]} ({1[1]} views)

    2. {2[0]} ({2[1]} views)

    3. {3[0]} ({3[1]} views)

    
    Connex.us\
    """.format(*form)
    message.send()

    self.respond(status="Sent a trending report rate at %s"%form[0])


class GetReportRateHandler(ServiceHandler):
  def post(self):
    self.respond(status="Grabbed report rate %r" % REPORT_RATE_MINS
                 ,rate=REPORT_RATE_MINS)

    
class SetReportRateHandler(ServiceHandler):
  def post(self):
    form = json.loads(self.request.body)
    global REPORT_RATE_MINS
    REPORT_RATE_MINS = form['rate']
    self.respond(status="Set report rate to %r" % REPORT_RATE_MINS)


class ViewAllStreamsHandler(ServiceHandler):
    def post(self):
      self.respond(streams=Stream.dump(),status="Grabbed all streams.")


class ViewStreamHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)        

        stream = Stream.getStream(form['stream_id'])
        if not stream:
            return self.respond(error="Stream %(stream_id)r does not exist." % form)

        outpages = []
        images = []
        for idx in form['page_range'].split(','):
            if not idx: break
            idx = int(idx)
            if idx >= len(stream.images): continue

            images.append(stream.images[idx])
            outpages.append(idx)

        self.respond(page_range=outpages,images=images
                     , status="Grabbed pages %r from stream %r" % (outpages,form['stream_id']))

    
class GetStreamsHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)
        user = User.getUser(form['user_id'])
        if not user:
            return self.respond(error="User %(user_id)r does not exist" % form)
        
        payload = {'user_streams': [Stream.getStream(stream_id).dumpStream() for stream_id in user.user_streams if Stream.exists(stream_id)]
                   , 'subscribed_streams': [Stream.getStream(stream_id).dumpStream() for stream_id in user.subscribed_streams if Stream.exists(stream_id)]
                   , 'status': "Grabbed streams for user %(user_id)r" % form}

        self.respond(**payload)


class CreateStreamHandler(ServiceHandler):        
    def post(self):
        form = json.loads(self.request.body)

        user = User.getUser(form['user_id'])
        if not user:
            return self.respond(error="User %(user_id)r does not exist" % form)

        if Stream.exists(form['stream_id']):
            return self.respond(error="Stream %(stream_id)r already exists" % form)

        # Create a new stream
        new_stream = Stream(user_id=form['user_id']
                            ,stream_id=form['stream_id']
                            ,last_add=None
                            ,tags=form['tags']
                            ,cover_url=form['cover_url'] if 'cover_url' in form else '')

        # Update the user's stream list and insert stream into db
        user.addStream(new_stream)

        d = search.Document(
            doc_id   = form['stream_id']
            ,fields   = [search.TextField(name='stream_id', value=form['stream_id'])
                         ,search.HtmlField(name='cover_url', value=form['cover_url'])]
            ,language = 'en')
        try:
            search.Index(name=INDEX_NAME).put(d)
        except search.Error as e:
            self.respond(error=str(e))

        self.respond(status="Created stream %(stream_id)r for user %(user_id)r." % form)
        

class DeleteImagesHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)
        
        stream = Stream.getStream(form['stream_id'])
        if not stream:
            return self.respond(error="Stream %(stream_id)r does not exist" % form)

        for image_id in set(form['images']) & set(stream.images):
            stream.removeImage(image_id)

        return self.respond(status="Images %(images)r removed" % form)


class DeleteStreamsHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)

        user = User.getUser(form['user_id'])
        if not user:
          return self.respond(status='User %(user_id)r not found' % form)

        for stream_id in set(form['streams']) & set(user.user_streams):
            user.removeStream(stream_id)

        self.respond(status="Deleted streams %(streams)r" % form)
        

class DeleteUserHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)        
        user = User.getUser(form['user_id'])
        if not user:
            return self.respond(status='User %(user_id)r not found' % form)
        user.delete()

        self.respond(status='Deleted user %(user_id)r' % form)

        
class CreateUserHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)
                     
        if User.exists(form['user_id']):
            return self.respond(error="User %(user_id)r already exists" % form)

        user = User(user_id=form['user_id'],user_pw=form['user_pw'])
        user.put()

        self.respond(status='Created new user %(user_id)r' %form)

classes = (User,Image,Stream)
funcs = ('dump','clear')
CallHandler = {'_'.join( (mthd,cls.__name__.lower()+'s') ): getattr(cls,mthd)
               for mthd,cls in itertools.product(funcs,classes)}

class TestServiceHandler(ServiceHandler):
    def post(self):
        form = json.loads(self.request.body)

        response = {}
        response['error'] = None
        response['status'] = 'OK'
        if form['service'] not in CallHandler:
            response['error'] = "Service %(service)r does not exist!" % form
            response['error'] += " Available services: %s" % CallHandler.keys()
        else:
            try:
                response.update({'response': CallHandler[form['service']]()})
            except KeyError as e:
                response['error'] = "Missing required information %s" % e
            except Exception as e:
                response['error'] = str(e)
        if response['error']: response['status'] = "Query failed."

        self.respond(**response)
