"""Simple HTTP server based on the asyncore / asynchat framework

Under asyncore, every time a socket is created it enters a table which is
scanned through select calls by the asyncore.loop() function

All events (a client connecting to a server socket, a client sending data, 
a server receiving data) is handled by the instances of classes derived 
from asyncore.dispatcher

Here the server is represented by an instance of the Server class

When a client connects to it, its handle_accept() method creates an
instance of RequestHandler, one for each HTTP request. It is derived
from asynchat.async_chat, a class where incoming data on the connection
is processed when a "terminator" is received. The terminator can be :
- a string : here we'll use the string \r\n\r\n to handle the HTTP request
line and the HTTP headers
- an integer (n) : the data is processed when n bytes have been read. This
will be used for HTTP POST requests

The data is processed by a method called found_terminator. In RequestHandler,
found_terminator is first set to handle_request_line to handle the HTTP
request line (including the decoding of the query string) and the headers. 
If the method is POST, terminator is set to the number of bytes to read
(the content-length header), and found_terminator is set to handle_post_data

After that, the handle_data() method is called and the connection is closed

Subclasses of RequestHandler only have to override the handle_data() method
"""

import asynchat, asyncore, socket, SimpleHTTPServer, select, urllib
import posixpath, sys, cgi, cStringIO, os, traceback, shutil
import pickle
import mutex
import threading
import time, shutil
import shelve

from datetime import datetime,timedelta

real_run = True;

my_shelf = shelve.open("udb_shelf",writeback=real_run);


snapshot_interval = 1800;

if real_run :
    port = 50609;
else :
    port = 8080;

version = '0.671';
min_version = '0.666';

class CI_dict(dict):
    """Dictionary with case-insensitive keys
    Replacement for the deprecated mimetools.Message class
    """

    def __init__(self, infile, *args):
        self._ci_dict = {}
        lines = infile.readlines()
        for line in lines:
            k,v=line.split(":",1)
            self._ci_dict[k.lower()] = self[k] = v.strip()
        self.headers = self.keys()
    
    def getheader(self,key,default=""):
        return self._ci_dict.get(key.lower(),default)
    
    def get(self,key,default=""):
        return self._ci_dict.get(key.lower(),default)
    
    def __getitem__(self,key):
        return self._ci_dict[key.lower()]
    
    def __contains__(self,key):
        return key.lower() in self._ci_dict
        
class socketStream:

    def __init__(self,sock):
        """Initiate a socket (non-blocking) and a buffer"""
        self.sock = sock
        self.buffer = cStringIO.StringIO()
        self.closed = 1   # compatibility with SocketServer
    
    def write(self, data):
        """Buffer the input, then send as many bytes as possible"""
        self.buffer.write(data)
        if self.writable():
            buff = self.buffer.getvalue()
            # next try/except clause suggested by Robert Brown
            try:
                    sent = self.sock.send(buff)
            except:
                    # Catch socket exceptions and abort
                    # writing the buffer
                    sent = len(data)

            # reset the buffer to the data that has not yet be sent
            self.buffer=cStringIO.StringIO()
            self.buffer.write(buff[sent:])
            
    def finish(self):
        """When all data has been received, send what remains
        in the buffer"""
        data = self.buffer.getvalue()
        # send data
        while len(data):
            while not self.writable():
                pass
            sent = self.sock.send(data)
            data = data[sent:]

    def writable(self):
        """Used as a flag to know if something can be sent to the socket"""
        return select.select([],[self.sock],[])[1]

request_count = 0;

class RequestHandler(asynchat.async_chat,
    SimpleHTTPServer.SimpleHTTPRequestHandler):

    protocol_version = "HTTP/1.1"
    MessageClass = CI_dict

    def __init__(self,conn,addr,server):
        global request_count;
        request_count = request_count + 1;
        print("outstanding requests = "+str(request_count));
        asynchat.async_chat.__init__(self,conn)
        self.client_address = addr
        self.connection = conn
        self.server = server
        # set the terminator : when it is received, this means that the
        # http request is complete ; control will be passed to
        # self.found_terminator
        self.set_terminator ('\r\n\r\n')
        self.rfile = cStringIO.StringIO()
        self.found_terminator = self.handle_request_line
        self.request_version = "HTTP/1.1"
        # buffer the response and headers to avoid several calls to select()
        self.wfile = cStringIO.StringIO()

    def close(self) :
        global request_count;
        asynchat.async_chat.close(self);
        request_count = request_count - 1;
        print("outstanding requests = "+str(request_count));        

    def collect_incoming_data(self,data):
        """Collect the data arriving on the connexion"""
        self.rfile.write(data)

    def prepare_POST(self):
        """Prepare to read the request body"""
        bytesToRead = int(self.headers.getheader('content-length'))
        # set terminator to length (will read bytesToRead bytes)
        self.set_terminator(bytesToRead)
        self.rfile = cStringIO.StringIO()
        # control will be passed to a new found_terminator
        self.found_terminator = self.handle_post_data
    
    def handle_post_data(self):
        """Called when a POST request body has been read"""
        self.rfile.seek(0)
        self.do_POST()
            
    def do_GET(self):
        """Begins serving a GET request"""
        # nothing more to do before handle_data()
        self.body = {}
        self.GET_headers = {};
        if len(self.path) > 0 :
            h=self.path.split('?');
            if len(h) > 1 :
                h = h[1].split('&');
                for H in h :
                    p = H.split('=');
                    if len(p) > 1 :
                        self.GET_headers[p[0]]=p[1];
                    else :
                        self.GET_headers[p[0]]="";
        self.handle_data()
        
    def do_POST(self):
        """Begins serving a POST request. The request data must be readable
        on a file-like object called self.rfile"""
        ctype, pdict = cgi.parse_header(self.headers.getheader('content-type'))
        self.body=cgi.FieldStorage(fp=self.rfile,
            headers=self.headers, environ = {'REQUEST_METHOD':'POST'},
            keep_blank_values = 1)
        self.handle_data()

    def handle_data(self):
        """Class to override"""
        print("handle_data");
        print(self.body);
        f = self.send_head()
        if f:
            self.copyfile(f, self.wfile)

    def handle_request_line(self):
        """Called when the http request line and headers have been received"""
        # prepare attributes needed in parse_request()
        self.rfile.seek(0)
        self.raw_requestline = self.rfile.readline()
        self.parse_request()

        if self.command in ['GET','HEAD']:
            # if method is GET or HEAD, call do_GET or do_HEAD and finish
            method = "do_"+self.command
            if hasattr(self,method):
                getattr(self,method)()
        elif self.command=="POST":
            # if method is POST, call prepare_POST, don't finish yet
            self.prepare_POST()
        else:
            self.send_error(501, "Unsupported method (%s)" %self.command)

    def end_headers(self):
        """Send the blank line ending the MIME headers, send the buffered
        response and headers on the connection, then set self.wfile to
        this connection
        This is faster than sending the response line and each header
        separately because of the calls to select() in socketStream"""
        if self.request_version != 'HTTP/0.9':
            self.wfile.write("\r\n")
        self.start_resp = cStringIO.StringIO(self.wfile.getvalue())
        self.wfile = socketStream(self.connection)
        self.copyfile(self.start_resp, self.wfile)

    def handle_error(self):
        traceback.print_exc(sys.stderr)
        self.close()

    def copyfile(self, source, outputfile):
        """Copy all data between two file objects
        Set a big buffer size"""
        shutil.copyfileobj(source, outputfile, length = 128*1024)

    def finish(self):
        """Send data, then close"""
        try:
            self.wfile.finish()
        except AttributeError: 
            # if end_headers() wasn't called, wfile is a StringIO
            # this happens for error 404 in self.send_head() for instance
            self.wfile.seek(0)
            self.copyfile(self.wfile, socketStream(self.connection))
        self.close()

class Server(asyncore.dispatcher):
    """Copied from http_server in medusa"""
    def __init__ (self, ip, port,handler):
        self.ip = ip
        self.port = port
        self.handler = handler
        asyncore.dispatcher.__init__ (self)
        self.create_socket (socket.AF_INET, socket.SOCK_STREAM)

        self.set_reuse_addr()
        self.bind ((ip, port))

        # lower this to 5 if your OS complains
        self.listen (1024)

    def handle_accept (self):
        try:
            conn, addr = self.accept()
        except socket.error:
            print("error");
            self.log_info ('warning: server accept() threw an exception', 'warning')
            return
        except TypeError:
            self.log_info ('warning: server accept() threw EWOULDBLOCK', 'warning')
            return
        # creates an instance of the handler class to handle the request/response
        # on the incoming connexion
        self.handler(conn,addr,self)

building_class_dict = {"armoury" : (0,True),
                       "arms" : (1,True),
                       "autoshop" : (2,True),
                       "bank" : (3,True),
                       "building" : (4,True),
                       "building1" : (5,True),
                       "carpark" : (6,False),
                       "cathedral" : (7,True),
                       "cemetary" : (8,False),
                       "church" : (9,True),
                       "cinema" : (10,True),
                       "club" : (11,True),
                       "factory" : (12,True),
                       "fire" : (13,True),
                       "fort" : (14,True),
                       "hospital" : (15,True),
                       "hotel" : (16,True),
                       "junkyard" : (17,True),
                       "library" : (18,True),
                       "mall" : (19,True),
                       "mansion" : (20,True),
                       "monument" : (21,False),
                       "museum" : (22,True),
                       "park" : (23,False),
                       "police" : (24,True),
                       "power" : (25,True),
                       "railway" : (26,True),
                       "school" : (27,True),
                       "stadium" : (28,True),
                       "street" : (29,False),
                       "towers" : (30,True),
                       "warehouse" : (31,True),
                       "wasteland" : (32,False),
                       "zoo" : (33,False),
                       "zoo1" : (34,True)}
                       

class UDSurvivorDBEntry :
    def __init__(self, i) :
        self.id = i;
        self.last_seen_time = 0;
        self.location = 0;
        self.last_seen_by = 0;
        self.known = False;
        self.color = '#000000';
    def dump(self) :
        print("player id " + str(self.id) + " last seen at " + str(self.location) + " ("+str(self.last_seen_time)+
              " by "+str(self.last_seen_by)+") "+str(self.known)+" "+str(self.color));
        

class UDSurvivorDB :
    def __init__(self) :
        self.db = {};
        self.db_mutex = mutex.mutex();
        
    def update_pos(self, id, timestamp, location, witness) :
        if self.db.has_key(id) :
            K = self.db[id];
        else :
            K = UDSurvivorDBEntry(id);
            self.db[id] = K;
        #K.dump();
        K.last_seen_time = timestamp;
        K.last_seen_location = location;
        K.last_seen_by = witness;
        return (id, K.known, K.color);

    def know_survivor(self, id, color) :
        if self.db.has_key(id) :
            K = self.db[id];
        else :
            K = UDSurvivorDBEntry(id);
            self.db[id] = K;
        #K.dump();
        K.known = True;
        K.color = color;

    def get_count(self) :
        return len(self.db);


if my_shelf.has_key("survivor_db") :
    survivor_db = my_shelf["survivor_db"];
    print("found surivor database with "+str(survivor_db.get_count())+" chars");
else :
    survivor_db = UDSurvivorDB();
    my_shelf["survivor_db"] = survivor_db;
    print("created survivor database");

class UDMapEntry :
    def __init__(self, p) :
        long_ago = datetime.utcnow() - timedelta(100,100,100);
        self.position = p;
        self.indoor_time = long_ago;
        self.indoor_user = 0;
        self.indoor_zombies = 0;
        self.indoor_survivors = 0;
        self.outdoor_time = long_ago;
        self.outdoor_user = 0;
        self.outdoor_zombies = 0;
        self.outdoor_survivors = 0;
        self.cade_time = long_ago;
        self.cade_user = 0;
        self.cade_level = -1;
        self.ruin_time = long_ago;
        self.ruin_user = 0;
        self.ruin = 0;
        self.building = 0;
        self.ruin_change_time = long_ago;

    def dump(self) :
        print(" " + str(self.position) + " " + str(self.indoor_zombies) + " " + str(self.indoor_survivors) + " " + str(self.indoor_time) + " " + str(self.indoor_user));
        print("    " + str(self.outdoor_zombies) + " " + str(self.outdoor_survivors) + " " + str(self.outdoor_time) + " " + str(self.outdoor_user));
        print("    " + str(self.cade_level) + " " + str(self.cade_time) + " " + str(self.cade_user));
        print("    " + str(self.building));

import re;

class UDMap :
    def __init__(self) :
        self.map_data = {};
        self.building_names = {};
        self.map_mutex = mutex.mutex();
        for i in range(10000) :
            self.map_data[i] = UDMapEntry(i);
        self.load();
        self.load_types();

    def building_name(self, pos) :
        return self.building_names[pos];
    
    def load_types(self) :
        try:
            line_pattern = re.compile('^(\w+)\s(\d+)\s(\d+)\s(.*)')
            f = open("map_data", "r");
            for line in f.readlines() :
                r = line_pattern.search(line);
                if r :
                    cl = building_class_dict[r.groups()[0]];
                    xpos = int(r.groups()[1]);
                    ypos = int(r.groups()[2]);
                    self.map_data[xpos*100+ypos].building = cl;
                    self.building_names[xpos*100+ypos] = r.groups()[3];
                else :
                    print("parse error " + line)
            f.close();
        except IOError:
            print("couldn't load map data");            

    def tasty_helper(self, x, y, t) :
        if x < 0 or x > 99 or y < 0 or y > 99 :
            return None;
        M = self.map_data[x*100+y];
        if M.cade_level != -1 and M.indoor_survivors > 0 and M.cade_level < 5 :
            if t - M.indoor_time < timedelta(days = 1) :
                return (x, y, M.cade_level, M.cade_time,
                        M.indoor_survivors, M.indoor_time, self.building_names[x*100+y]);
        return None;

    def find_tasties(self, pos, current_time) :
        y = pos % 100;
        x = (pos - y)/100;
        resp = [];
        for d in range(1,9) :
            for xo in range(-d,d) :
                t = self.tasty_helper(x+xo, y-d, current_time);
                if t != None :
                    resp.append(t);
                t = self.tasty_helper(x-xo, y+d, current_time);
                if t != None :
                    resp.append(t);
                t = self.tasty_helper(x-d, y-xo, current_time);
                if t != None :
                    resp.append(t);
                t = self.tasty_helper(x+d, y+xo, current_time);
                if t != None :
                    resp.append(t);
            if len(resp) > 5 :
                return resp;
        return resp
    
    def load(self) :
        try:
            f = open("udmap.dat", "r");
            self.map_data = pickle.load(f);
            f.close();
        except IOError:
            print("couldn't load saved data");
        return
        
    def get(self, p) :
        return self.map_data[p];

    def save_locked(self) :
        print "saving map"
        try:
            f = open("udmap.dat", "w");
            pickle.dump(self.map_data, f);
            f.close()
        except IOError:
            print("couldn't save!!!!\n");
        self.map_mutex.unlock();
        return

    def save(self) :
        self.map_mutex.lock(UDMap.save_locked, self);

    def save_snapshot_locked(self) :
        try:
            shutil.copyfile('udmap.dat', 'logs/udmap-'+str(datetime.utcnow())+'.dat');
        except IOError:
            print("couldn't copy file");
        self.map_mutex.unlock();

    def save_snapshot(self) :
        self.map_mutex.lock(UDMap.save_snapshot_locked, self);
#        self.save();

#    def set(x,y,data) :
#        
        
ud_map = UDMap();    


class SnapshotThread ( threading.Thread ):
    def run ( self ):
        global snapshot_interval;
        self.go = True;
        #time.sleep(snapshot_interval);
        while self.go :
            ud_map.save_snapshot();
            # save a copy of the shelf :
            try:
                shutil.copyfile('udb_shelf', 'logs/udb_shelf-'+str(datetime.utcnow()));
            except IOError:
                print("couldn't copy file");
            time.sleep(snapshot_interval);

    def stop_saving ( self ) :
        self.go = False;

burb_dict = {'suburb=dakerstown':(0,0),
             'suburb=jensentown':(1,0),
             'suburb=quarlesbank':(2,0),
             'suburb=west+boundwood':(3,0),
             'suburb=east+boundwood':(4,0),
             'suburb=lamport+hills':(5,0),
             'suburb=chancelwood':(6,0),
             'suburb=earletown':(7,0),
             'suburb=rhodenbank':(8,0),
             'suburb=dulston':(9,0),

             'suburb=roywood':(0,1),
             'suburb=judgewood':(1,1),
             'suburb=gatcombeton':(2,1),
             'suburb=shuttlebank':(3,1),
             'suburb=yagoton':(4,1),
             'suburb=millen+hills':(5,1),
             'suburb=raines+hills':(6,1),
             'suburb=pashenton':(7,1),
             'suburb=rolt+heights':(8,1),
             'suburb=pescodside':(9,1),
             
             'suburb=peddlesden+village':(0,2),
             'suburb=chudleyton':(1,2),
             'suburb=darvall+heights':(2,2),
             'suburb=eastonwood':(3,2),
             'suburb=brooke+hills':(4,2),
             'suburb=shearbank':(5,2),
             'suburb=huntley+heights':(6,2),
             'suburb=santlerville':(7,2),
             'suburb=gibsonton':(8,2),
             'suburb=dunningwood':(9,2),
             
             'suburb=dunell+hills':(0,3),
             'suburb=west+becktown':(1,3),
             'suburb=east+becktown':(2,3),
             'suburb=richmond+hills':(3,3),
             'suburb=ketchelbank':(4,3),
             'suburb=roachtown':(5,3),
             'suburb=randallbank':(6,3),
             'suburb=heytown':(7,3),
             'suburb=spracklingbank':(8,3),
             'suburb=paynterton':(9,3),

             'suburb=owsleybank':(0,4),
             'suburb=molebank':(1,4),
             'suburb=lukinswood':(2,4),
             'suburb=havercroft':(3,4),
             'suburb=barrville':(4,4),
             'suburb=ridleybank':(5,4),
             'suburb=pimbank':(6,4),
             'suburb=peppardville':(7,4),
             'suburb=pitneybank':(8,4),
             'suburb=starlingtown':(9,4),

             'suburb=grigg+heights':(0,5),
             'suburb=reganbank':(1,5),
             'suburb=lerwill+heights':(2,5),
             'suburb=shore+hills':(3,5),
             'suburb=galbraith+hills':(4,5),
             'suburb=stanbury+village':(5,5),
             'suburb=roftwood':(6,5),
             'suburb=edgecombe':(7,5),
             'suburb=pegton':(8,5),
             'suburb=dentonside':(9,5),

             'suburb=crooketon':(0,6),
             'suburb=mornington':(1,6),
             'suburb=north+blythville':(2,6),
             'suburb=brooksville':(3,6),
             'suburb=mockridge+heights':(4,6),
             'suburb=shackleville':(5,6),
             'suburb=tollyton':(6,6),
             'suburb=crowbank':(7,6),
             'suburb=vinetown':(8,6),
             'suburb=houldenbank':(9,6),

             'suburb=nixbank':(0,7),
             'suburb=wykewood':(1,7),
             'suburb=south+blythville':(2,7),
             'suburb=greentown':(3,7),
             'suburb=tapton':(4,7),
             'suburb=kempsterbank':(5,7),
             'suburb=wray+heights':(6,7),
             'suburb=gulsonside':(7,7),
             'suburb=osmondville':(8,7),
             'suburb=penny+heights':(9,7),

             'suburb=foulkes+village':(0,8),
             'suburb=ruddlebank':(1,8),
             'suburb=lockettside':(2,8),
             'suburb=dartside':(3,8),
             'suburb=kinch+heights':(4,8),
             'suburb=west+grayside':(5,8),
             'suburb=east+grayside':(6,8),
             'suburb=scarletwood':(7,8),
             'suburb=pennville':(8,8),
             'suburb=fryerbank':(9,8),
             
             'suburb=new+arkham':(0,9),
             'suburb=old+arkham':(1,9),
             'suburb=spicer+hills':(2,9),
             'suburb=williamsville':(3,9),
             'suburb=buttonville':(4,9),
             'suburb=wyke+hills':(5,9),
             'suburb=hollomstown':(6,9),
             'suburb=danversbank':(7,9),
             'suburb=whittenside':(8,9),
             'suburb=miltown':(9,9)}


def age(now, before) :
    delta = now - before;
    return str(delta.seconds + 86400*delta.days)

db_submit_dict = {};
last_ip_limit_reset_time = datetime.utcnow();
    

class UDRequestHandler(RequestHandler) :
    foo = 1;

    def process_datum(self, x) :
        p = x.split(':');
        if len(x) == 0 : # ignore empty data
            return
        if len(p) != 3 :
            self.udbrain_error("bad datum size");
            self.send_response(501);
            return
        try:
            datum = map(int, p);
#            print(datum)
            if datum[0] < 0 or datum[0] > 9999 :
                self.udbrain_error("bad datum location " + str(datum[0]));
                self.send_response(501);
                return
            M = ud_map.get(datum[0]);
#            M.dump();
            if datum[1] == 1 :
                # barricades
                M.cade_level = datum[2];
                M.cade_time = self.timestamp;
                M.cade_user = self.userid;
            elif datum[1] == 2 :
                # outdoor zombies
                M.outdoor_zombies = datum[2];
                M.outdoor_time = self.timestamp;
                M.outdoor_user = self.userid;
            elif datum[1] == 3 :
                # indoor zombies
                M.indoor_zombies = datum[2];
                M.indoor_time = self.timestamp;
                M.indoor_user = self.userid;
            elif datum[1] == 4 :
                if (M.ruin != 1) and datum[2] == 1 and M.indoor_survivors > 0 :
                    # the building has become ruined since last observation
                    # if survivors were inside the building, they must have
                    # been removed for the building to have become ruined.
                    # Of course, we may have just read a datum that says
                    # survivors are present (from the same set of observations)
                    # so we only do this if the survivor data is older than
                    # a minute (long, sure, but we should be careful with
                    # parallel observations, etc)
                    # We can't make the survivor count observation more recent
                    # without violating some trust - after all, the survivors
                    # observed could have left immediately after being seen.
                    # So we just set the count to 0.
                    if self.timestamp - M.indoor_time > timedelta(seconds=60) :
                        M.indoor_survivors = 0;
                if (M.ruin != datum[2]) :
                    # ruin status has changed :
                    M.ruin_change_time = self.timestamp;
                M.ruin = datum[2];
                M.ruin_time = self.timestamp;
                M.ruin_user = self.userid;
            elif datum[1] == 5 :
                M.indoor_survivors = datum[2];
                M.indoor_time = self.timestamp;
                M.indoor_user = self.userid;
            else :
                self.udbrain_error("bad field " + str(datum[1]));
                self.send_response(501);
                return
#            print(M);
#            M.dump();
        except ValueError:
            self.udbrain_error("bad datum " + x);
            self.send_response(501);
            return


    def handle_survivor_data(self, player_id, location) :
        self.survivor_list = [];
        if self.body.has_key('survivors') :
            # print(self.body.getvalue('survivors'));
            try:
                survivor_id_list = map(int, self.body.getvalue('survivors').split(','));
            except ValueError:
                self.udbrain_error("bad survivor ids " + self.body.getvalue('survivors'));
                return
                
            for x in survivor_id_list :
                self.survivor_list.append(survivor_db.update_pos(x, self.timestamp, location, player_id));
        
    def handle_incoming_data(self) :
        if not self.body.has_key('data') :
            self.udbrain_error("no data field");
            self.send_response(501);
            return
        data_data = self.body.getvalue('data').split('|');
        # print(data_data);
        def U(x) : return self.process_datum(x)
        map(U, data_data);

    def get_suburb_data(self, coords) :
        self.send_response(200)
        self.send_header("Content-type", "text/plain");
        self.end_headers()
        self.wfile.write(version+"\n");
        for x in range(10) :
            for y in range(10) :
                coord = y + coords[1]*10 + 100*x + coords[0]*1000;
                M = ud_map.get(coord);
                report = [str(coord), age(self.timestamp, M.cade_time), "1", str(M.cade_level),
                          age(self.timestamp, M.indoor_time), str(M.indoor_zombies),
                          age(self.timestamp, M.outdoor_time), str(M.outdoor_zombies),
                          age(self.timestamp, M.ruin_time), str(M.ruin),
                          str(M.indoor_survivors)];
                self.wfile.write(":".join(report) + "\n");
#        self.end_headers
        return

    def handle_graph(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html");
        self.end_headers()
        self.wfile.write("<html><head></head><body>");
        self.wfile.write("<table>");
        cade_colors = {-1:'#ffffff', 1:'#800000', 2:'#ff0000', 3:'#aa3300',
                       4:'#ff8000', 5:'#ffff00', 6:'#00ff00', 7:'#00aa20',
                       8:'#008080', 9:'#0000ff'};
        if not self.GET_headers.has_key('type') :
            graph_type = 0;
        else :
            try:
                graph_type = int(self.GET_headers['type']);
            except ValueError:
                graph_type = 0;
        
        for sy in range(10) :
            self.wfile.write("<tr>\n");
            for sx in range(10) :
                self.wfile.write("<td>");
                self.wfile.write("<table border=1>\n");
                for y in range(10) :
                    self.wfile.write("<tr>\n");
                    row = "";
                    for x in range(10) :
                        M = ud_map.get(y + sy*10 + 100*x + sx*1000);
                        #M.dump()
                        if M.building[1] :
                            color = '#606060'
                        else :
                            color = '#ffffff';
                        if graph_type == 0 :
                            if self.timestamp - M.ruin_time < timedelta(days = 7) :
                                if M.ruin == 1 :
                                    color = '#800000'
                                elif M.cade_level != -1 :
                                    # if the ruin status has changed after the
                                    # last barricade update, we no longer have
                                    # reliable information, so we leave it gray
                                    if M.ruin_change_time < M.cade_time :
                                        color = cade_colors[M.cade_level];
                        elif graph_type == 1 :
                            if self.timestamp - M.indoor_time < timedelta(days = 7) :
                                if M.indoor_survivors == 0 :
                                    color = '#000000';
                                elif M.indoor_survivors == 1 :
                                    color = '#00a000';
                                elif M.indoor_survivors < 4 :
                                    color = '#00ff00';
                                elif M.indoor_survivors < 8 :
                                    color = '#80ff00';
                                elif M.indoor_survivors < 12 :
                                    color = '#eeee00';
                                elif M.indoor_survivors < 20 :
                                    color = '#ff8000';
                                elif M.indoor_survivors < 30 :
                                    color = '#ff0000';
                                elif M.indoor_survivors < 45 :
                                    color = '#ff0080';
                                else :
                                    color = '#ff00ff';
                        row = row + '<td width=5px style="height: 5px; background: ' + color +';"></td>';
                        #self.wfile.write('<td width=5px style="height: 5px; background: ' + color +';">');
                        #self.wfile.write("</td>");
                    self.wfile.write(row)
                    self.wfile.write("</tr>\n");
                self.wfile.write("</table>");
                self.wfile.write("</td>\n");
            self.wfile.write("</tr>\n");
        self.wfile.write("</table>\n");
        self.wfile.write("</body></html>\n");
        
    def handle_data(self):
        self.timestamp = datetime.utcnow();
        self.my_path = self.path.split('?');
        if len(self.my_path) == 0 :
            self.udbrain_error("no path");
            self.send_response(501);
        if self.my_path[0] == '/ud_xml' :
            self.handle_udbrainmap();
        elif self.my_path[0] == '/udb' :
            self.handle_udbrain();
        elif self.my_path[0] == '/udbq' :
            self.handle_udbrain_query();
        elif self.my_path[0] == '/udgraph' :
            self.handle_graph();
        elif self.my_path[0] == '/udknowsurvivor' :
            self.handle_know_survivor();
        else :
            self.send_response(404);
        self.finish();

    def handle_know_survivor(self) :
        if not self.GET_headers.has_key('id') :
            self.udbrain_error("Need id");
            self.send_response(501);
            return
        if not self.GET_headers.has_key('color') :
            self.udbrain_error("Need color")
            self.send_response(501);
            return
        try:
            playerid = int(self.GET_headers['id']);
        except ValueError:
            self.udbrain_error("Bad id");
            self.send_response(501);
            return
        survivor_db.know_survivor(playerid, self.GET_headers['color']);
        self.send_response(200);

    def handle_udbrainmap(self) :
        if len(self.my_path) != 2 :
            self.udbrain_error("error - no request");
            self.send_response(501);
            return
        if not burb_dict.has_key(self.my_path[1]) :
            self.udbrain_error("bad suburb " + self.my_path[1]);
            self.send_response(501);
            return
        self.get_suburb_data(burb_dict[self.my_path[1]]);

    def respond_with_data(self, player) :
        global version
        if len(self.my_path) < 2 :
            return
        squares = self.my_path[1].split('&');
        try:
            squares = map(int, squares);
        except ValueError:
            self.udbrain_error("bad location request " + str(squares));
            self.send_response(501);
            return
        response = ['v' + version];
        for x in squares :
            if x < 0 or x > 9999 :
                self.udbrain_error("bad location " + str(x))
                self.send_response(501);
                return
            M = ud_map.get(x);
            #            M.dump()
            if M.cade_level != -1 :
                response.append(str(x)+':'+age(self.timestamp, M.cade_time)+':1:'+str(M.cade_level)+':'+age(self.timestamp, M.indoor_time)+':'+str(M.indoor_survivors))
        if player :
            for x in self.survivor_list :
                if x[1] :
                    response.append("S:"+str(x[0])+":"+x[2]);
            tasties = ud_map.find_tasties(self.user_pos, self.timestamp);
            for x in tasties :
                response.append("T:"+str(x[0])+":"+str(x[1])+":"+str(x[2])+":"+
                                age(self.timestamp, x[3])+":"+str(x[4])+":"+
                                age(self.timestamp, x[5])+":"+x[6]);
        self.send_response(200);
        self.end_headers();
        self.wfile.write("|".join(response))
        self.wfile.write('\n');
        return

    def log_request(self, code='-', size='-') :
        print('request : ' + self.client_address[0] + ' ' + self.path);

    def udbrain_error(self, msg) :
        print('ERROR ' + self.client_address[0] + ' ' + self.path);
        print('   : ' + msg);

    def handle_udbrain_query(self) :
        global version
        # we send more information here than we do in respond_with_data:
        if len(self.my_path) < 2 :
            return
        squares = self.my_path[1].split('&');
        try:
            squares = map(int, squares);
        except ValueError:
            self.udbrain_error("bad location request " + str(squares));
            self.send_response(501);
            return
        response = [version];
        for x in squares :
            if x < 0 or x > 9999 :
                self.udbrain_error("bad location " + str(x))
                self.send_response(501);
                return
            M = ud_map.get(x);
            #            M.dump()
            response.append(str(x)+':'+age(self.timestamp, M.cade_time)+':'+str(M.cade_level)+':'+
                            age(self.timestamp, M.indoor_time)+':'+str(M.indoor_survivors)+':'+
                            str(M.indoor_zombies)+':'+age(self.timestamp, M.ruin_time)+':'+str(M.ruin));
        self.send_response(200);
        self.end_headers();
        self.wfile.write("|".join(response))
        self.wfile.write('\n');
        return

#    def log_message(self, msg) :
#        print(msg);
    
    def handle_udbrain(self) :
        global min_version;
        global last_ip_limit_reset_time;
        if self.timestamp - last_ip_limit_reset_time > timedelta(1) :
            db_submit_dict.clear();
            last_ip_limit_reset_time = self.timestamp;
        addr = self.client_address[0];
        if not db_submit_dict.has_key(addr) :
            db_submit_dict[addr] = 0;
        db_submit_dict[addr] = db_submit_dict[addr] + 1;
        if db_submit_dict[addr] > 800 :
            self.udbrain_error("too many submissions");
            self.respond_with_data(False);
            return
        if not self.body.has_key('user') :
            self.udbrain_error("no user field");
            self.send_response(501);
            return
        user_data = self.body.getvalue('user').split(':');
        if len(user_data) != 4 :
            self.udbrain_error("wrong user data size");
            self.send_response(501);
            return
        if user_data[1] < min_version :
            self.udbrain_error("wrong UDBrain version " + user_data[1]);
            self.send_response(501);
            return
        try:
            self.userid = int(user_data[0]);
            self.user_pos = int(user_data[2]);
            if self.user_pos < 0 or self.user_pos > 9999 :
                self.udbrain_error("bad location " + self.user_pos);
                self.send_response(501);
                return
            
            if not ( user_data[3] == '1' or  user_data[3] == '2' or user_data[3] == '3' ) :
                self.udbrain_error("bad location type " + user_data[3]);
                self.send_response(501);
                return

            self.handle_incoming_data();
            self.handle_survivor_data(self.userid, self.user_pos);
            self.respond_with_data(True);
            
        except ValueError:
            self.udbrain_error("bad data");
            print(user_data);
            self.send_response(501);
            return
        
#        .split(':');
#        self.userid = user_data[0];
        
        
if __name__=="__main__":
    # launch the server on the specified port
    s=Server('', port, UDRequestHandler)
    print "SimpleAsyncHTTPServer running on port %s" %port
    if real_run :
        st = SnapshotThread();
        st.setDaemon(True);
        st.start();
        
    try:
        asyncore.loop()
    except KeyboardInterrupt:
        if real_run :
            st.stop_saving();
            print("syncing shelf");
            my_shelf.close();
            ud_map.save()
        print "Crtl+C pressed. Shutting down."
