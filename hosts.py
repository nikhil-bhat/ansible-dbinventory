#!/usr/bin/env python
from pprint import pprint
from sqlalchemy.orm import relationship


# blueacorn host manager 
# https://bitbucket.org/zzzeek/sqlalchemy/wiki/UsageRecipes/SymmetricEncryption


'''
BlueAcorn external inventory script
===================================

Generates Ansible inventory backed by an sqlite database

Based on https://github.com/geerlingguy/ansible-for-devops/tree/master/dynamic-inventory/digitalocean

In addition to the --list and --host options used by Ansible, there are options
for managing and printing passwords. Passwords are stored using AES symmetric
encryption and can only be retrieved by providing the correct passphrase. 

The --pretty (-p) option pretty-prints the output for better human readability.

----
@todo
The following groups are generated from --list:
 - ID    (droplet ID)
 - NAME  (droplet NAME)
 - image_ID
 - image_NAME
 - distro_NAME  (distribution NAME from image)
 - region_ID
 - region_NAME
 - size_ID
 - size_NAME
 - status_STATUS

When run against a specific host, this script returns the following variables:
 - do_created_at
 - do_distroy
 - do_id
 - do_image
 - do_image_id
 - do_ip_address
 - do_name
 - do_region
 - do_region_id
 - do_size
 - do_size_id
 - do_status

-----
```
usage: digital_ocean.py [-h] [--list] [--host HOST] [--all]
                                 [--droplets] [--regions] [--images] [--sizes]
                                 [--ssh-keys] [--domains] [--pretty]
                                 [--cache-path CACHE_PATH]
                                 [--cache-max_age CACHE_MAX_AGE]
                                 [--refresh-cache] [--client-id CLIENT_ID]
                                 [--api-key API_KEY]

Produce an Ansible Inventory file based on DigitalOcean credentials

optional arguments:
  -h, --help            show this help message and exit
  --list                List all active Droplets as Ansible inventory
                        (default: True)
  --host HOST           Get all Ansible inventory variables about a specific
                        Droplet
  --all                 List all DigitalOcean information as JSON
  --droplets            List Droplets as JSON
  --regions             List Regions as JSON
  --images              List Images as JSON
  --sizes               List Sizes as JSON
  --ssh-keys            List SSH keys as JSON
  --domains             List Domains as JSON
  --pretty, -p          Pretty-print results
  --cache-path CACHE_PATH
                        Path to the cache files (default: .)
  --cache-max_age CACHE_MAX_AGE
                        Maximum age of the cached items (default: 0)
  --refresh-cache       Force refresh of cache by making API requests to
                        DigitalOcean (default: False - use cache files)
  --client-id CLIENT_ID, -c CLIENT_ID
                        DigitalOcean Client ID
  --api-key API_KEY, -a API_KEY
                        DigitalOcean API Key
```

'''

######################################################################

import os
import sys
import re
import argparse
from time import time
import ConfigParser

try:
    import json
except ImportError:
    import simplejson as json

try:
    from sqlalchemy import create_engine, Column, Integer, String, Enum, ForeignKey
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import Session
except ImportError, e:
    print "failed=True msg='`sqlalchemy` library required for this script'"
    sys.exit(1)


try:
    from Crypto.Cipher import AES
except ImportError, e:
    print "failed=True msg='`pycrypto` library required for this script'"
    sys.exit(1)
    


class BlueAcornInventory(object):

    ###########################################################################
    # Main execution path
    ###########################################################################

    def __init__(self):
        ''' Main execution path '''

        # BlueAcornInventory data
        self.data = {}  # All DigitalOcean data
        self.inventory = {}  # Ansible Inventory
        self.index = {}  # Various indices of Droplet metadata
        
         # Read settings, environment variables, and CLI arguments
        self.read_environment()
        self.read_cli_args()
        
        
        # initialize the database
        self.db_engine = None
        self.db_session = None
        self.database_initialize()
        
        
        sys.exit(0)



        # Verify credentials were set
        if not hasattr(self, 'client_id') or not hasattr(self, 'api_key'):
            print '''Could not find values for DigitalOcean client_id and api_key.
They must be specified via either ini file, command line argument (--client-id and --api-key),
or environment variables (DO_CLIENT_ID and DO_API_KEY)'''
            sys.exit(-1)

        # env command, show DigitalOcean credentials
        if self.args.env:
            print "DO_CLIENT_ID=%s DO_API_KEY=%s" % (self.client_id, self.api_key)
            sys.exit(0)

        # Manage cache
        self.cache_filename = self.cache_path + "/ansible-digital_ocean.cache"
        self.cache_refreshed = False

        if not self.args.force_cache and self.args.refresh_cache or not self.is_cache_valid():
            self.load_all_data_from_digital_ocean()
        else:
            self.load_from_cache()
            if len(self.data) == 0:
                if self.args.force_cache:
                    print '''Cache is empty and --force-cache was specified'''
                    sys.exit(-1)
                self.load_all_data_from_digital_ocean()
            else:
                # We always get fresh droplets for --list, --host, --all, and --droplets
                # unless --force-cache is specified
                if not self.args.force_cache and (
                   self.args.list or self.args.host or self.args.all or self.args.droplets):
                    self.load_droplets_from_digital_ocean()

        # Pick the json_data to print based on the CLI command
        if self.args.droplets:   json_data = { 'droplets': self.data['droplets'] }
        elif self.args.regions:  json_data = { 'regions':  self.data['regions'] }
        elif self.args.images:   json_data = { 'images':   self.data['images'] }
        elif self.args.sizes:    json_data = { 'sizes':    self.data['sizes'] }
        elif self.args.ssh_keys: json_data = { 'ssh_keys': self.data['ssh_keys'] }
        elif self.args.domains:  json_data = { 'domains':  self.data['domains'] }
        elif self.args.all:      json_data = self.data

        elif self.args.host:     json_data = self.load_droplet_variables_for_host()
        else:  # '--list' this is last to make it default
                                 json_data = self.inventory

        if self.args.pretty:
            print json.dumps(json_data, sort_keys=True, indent=2)
        else:
            print json.dumps(json_data)
        # That's all she wrote...


    ###########################################################################
    # Script configuration
    ###########################################################################

    def read_environment(self):
        ''' Reads the settings from environment variables '''
        # Setup credentials
        if os.getenv("DBINVENTORY_PATH"): self.db_path = os.getenv("DBINVENTORY_PATH")
        if os.getenv("DBINVENTORY_SECRET"): self.db_secret = os.getenv("DBINVENTORY_SECRET")


    def read_cli_args(self):
        ''' Command line argument processing '''
        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file from an sqlite database')
        
        parser.add_argument('--pretty', '-p', action='store_true', help='Pretty-print results')
        
        
        parser.add_argument('--db-path', '-d', action='store', help='Path to Hosts Database File, defaults to DBINVENTORY_PATH environment variable if set, or "<current working directory>/hosts.sqlite3"')
        
        parser.add_argument('--db-create', '-c', action='store_true', help='When set, attempt to create the database if it does not already exist')
        parser.add_argument('--db-export', '-e', action='store_true', help='Export groups, tags, and hosts as JSON')
        parser.add_argument('--db-import', '-i', action='store', help='Pathname to JSON file containing groups, tags, and hosts to import.')
        parser.add_argument('--db-secret', '-s', action='store', help='Database Secret Key for host password encryption, defaults to DBINVENTORY_SECRET environment variable')
        
        parser.add_argument('--list', action='store_true', help='List all active Hosts (default: True)')
        parser.add_argument('--host', action='store', help='Get all Ansible inventory variables about a specific Host')

        parser.add_argument('--manage', '-m', action='store_true', help='Manage Hosts')
        parser.add_argument('--add', '-a', action='store_true', help='Add Host')
       
        self.args = parser.parse_args()

        if self.args.db_path: self.db_path = self.args.db_path
        if self.args.db_secret: self.db_secret = self.args.db_secret

        # Make --list default if none of the other commands are specified
        if (not self.args.manage and not self.args.add):
                self.args.list = True


    ###########################################################################
    # Data Management
    ###########################################################################
    
    def database_initialize(self):
        
        if not hasattr(self, 'db_path'):
            self.db_path = os.path.dirname(os.path.realpath(__file__)) + '/hosts.sqlite3'  
            
        if not os.path.isfile(self.db_path):
            if(self.args.db_create):
                self.database_create_tables()
            else:
                print "\nDatabase %s does not exist.\n\nSpecify a location, or use --db-create to start a new database" % (self.db_path)
                sys.exit(-1)
                
        
        if self.args.db_import:
            self.database_import(self.args.db_import)
        
                
        return self.database_get_session()
    
    def database_create_tables(self):
        engine = self.database_get_engine()
        Base.metadata.create_all(engine)
        
    def database_get_session(self):
        if not self.db_session:
            self.db_session = Session(self.database_get_engine())
            
        return self.db_session
        
    def database_get_engine(self):
        if not self.db_engine:
            self.db_engine = create_engine('sqlite:///' + self.db_path, echo=False)
        
        return self.db_engine 
    
    # initial tags & groups
    def database_import(self, filename):
        
        if not os.path.isfile(filename):
            filename = os.path.dirname(os.path.realpath(__file__)) + filename
            if not os.path.isfile(filename):
                print "\nImport File '%s' does not exist." % (filename)
                sys.exit(-1)
        
        with open(filename) as data_file:    
            data = json.load(data_file)
            
        
        for key in ['groups','tags','hosts']:
            if key in data:
                method = getattr(self,"add_" + key[:-1])
                for obj in data[key]:
                    method(obj)
        
        return
    
    
    def add_group(self, obj):
        Record = self.get_group(name=obj['name'])
        
        if not Record:
            db = self.database_get_session()
            Record = TagGroup(name=obj['name'], selection_type=obj['type'])
            db.add(Record)
            db.commit()
    
        return Record
    
    def add_host(self, obj):
        Record = self.get_host(host=obj['host'])
        
        if not Record:
            db = self.database_get_session()
            Record = Host(host=obj['host'])
            
            for key in ['host_name','ssh_user','ssh_port']:
                if key in obj:
                    setattr(Record,key,obj[key])
            
            db.add(Record)
            db.commit()
            
        if 'tags' in obj:
            db = self.database_get_session()
            for tag_name in obj['tags']:
                TagRecord = self.get_tag(name=tag_name)
                if TagRecord:
                    Record.tags.append(TagRecord)
            db.commit()
    
        return Record
    
    def add_tag(self, obj):
        Record = self.get_tag(name=obj['name'])
        group = self.get_group(name=obj['group'])
        
        if not group:
            print "could not add tag `%s`, group `%s` not found" % (obj['name'], obj['group'])
            sys.exit(-1)
        
        
        if not Record:
            db = self.database_get_session()
            Record = Tag(name=obj['name'],group_id=group.id)
            db.add(Record)
            db.commit()
            
        return Record
    
   
    def get_group(self, **kwargs):
        return self.database_get_session().query(TagGroup).filter_by(**kwargs).first()
    
    def get_host(self, **kwargs):
        return self.database_get_session().query(Host).filter_by(**kwargs).first()
            
    def get_tag(self, **kwargs):
        return self.database_get_session().query(Tag).filter_by(**kwargs).first()
    

    def load_all_data_from_digital_ocean(self):
        ''' Use dopy to get all the information from DigitalOcean and save data in cache files '''
        manager = DoManager(self.client_id, self.api_key)

        self.data = {}
        self.data['droplets'] = self.sanitize_list(manager.all_active_droplets())
        self.data['regions'] = self.sanitize_list(manager.all_regions())
        self.data['images'] = self.sanitize_list(manager.all_images(filter=None))
        self.data['sizes'] = self.sanitize_list(manager.sizes())
        self.data['ssh_keys'] = self.sanitize_list(manager.all_ssh_keys())
        self.data['domains'] = self.sanitize_list(manager.all_domains())

        self.index = {}
        self.index['region_to_name'] = self.build_index(self.data['regions'], 'id', 'name')
        self.index['size_to_name'] = self.build_index(self.data['sizes'], 'id', 'name')
        self.index['image_to_name'] = self.build_index(self.data['images'], 'id', 'name')
        self.index['image_to_distro'] = self.build_index(self.data['images'], 'id', 'distribution')
        self.index['host_to_droplet'] = self.build_index(self.data['droplets'], 'ip_address', 'id', False)

        self.build_inventory()

        self.write_to_cache()


    def load_droplets_from_digital_ocean(self):
        ''' Use dopy to get droplet information from DigitalOcean and save data in cache files '''
        manager = DoManager(self.client_id, self.api_key)
        self.data['droplets'] = self.sanitize_list(manager.all_active_droplets())
        self.index['host_to_droplet'] = self.build_index(self.data['droplets'], 'ip_address', 'id', False)
        self.build_inventory()
        self.write_to_cache()


    def build_index(self, source_seq, key_from, key_to, use_slug=True):
        dest_dict = {}
        for item in source_seq:
            name = (use_slug and item.has_key('slug')) and item['slug'] or item[key_to]
            key = item[key_from]
            dest_dict[key] = name
        return dest_dict


    def build_inventory(self):
        '''Build Ansible inventory of droplets'''
        self.inventory = {}

        # add all droplets by id and name
        for droplet in self.data['droplets']:
            dest = droplet['ip_address']

            self.inventory[droplet['id']] = [dest]
            self.push(self.inventory, droplet['name'], dest)
            self.push(self.inventory, 'region_' + droplet['region_id'], dest)
            self.push(self.inventory, 'image_' + droplet['image_id'], dest)
            self.push(self.inventory, 'size_' + droplet['size_id'], dest)
            self.push(self.inventory, 'status_' + droplet['status'], dest)

            region_name = self.index['region_to_name'].get(droplet['region_id'])
            if region_name:
                self.push(self.inventory, 'region_' + region_name, dest)

            size_name = self.index['size_to_name'].get(droplet['size_id'])
            if size_name:
                self.push(self.inventory, 'size_' + size_name, dest)

            image_name = self.index['image_to_name'].get(droplet['image_id'])
            if image_name:
                self.push(self.inventory, 'image_' + image_name, dest)

            distro_name = self.index['image_to_distro'].get(droplet['image_id'])
            if distro_name:
                self.push(self.inventory, 'distro_' + distro_name, dest)


    def load_droplet_variables_for_host(self):
        '''Generate a JSON response to a --host call'''
        host = self.to_safe(str(self.args.host))

        if not host in self.index['host_to_droplet']:
            # try updating cache
            if not self.args.force_cache:
                self.load_all_data_from_digital_ocean()
            if not host in self.index['host_to_droplet']:
                # host might not exist anymore
                return {}

        droplet = None
        if self.cache_refreshed:
            for drop in self.data['droplets']:
                if drop['ip_address'] == host:
                    droplet = self.sanitize_dict(drop)
                    break
        else:
            # Cache wasn't refreshed this run, so hit DigitalOcean API
            manager = DoManager(self.client_id, self.api_key)
            droplet_id = self.index['host_to_droplet'][host]
            droplet = self.sanitize_dict(manager.show_droplet(droplet_id))
       
        if not droplet:
            return {}

        # Put all the information in a 'do_' namespace
        info = {}
        for k, v in droplet.items():
            info['do_' + k] = v

        # Generate user-friendly variables (i.e. not the ID's) 
        if droplet.has_key('region_id'):
            info['do_region'] = self.index['region_to_name'].get(droplet['region_id'])
        if droplet.has_key('size_id'):
            info['do_size'] = self.index['size_to_name'].get(droplet['size_id'])
        if droplet.has_key('image_id'):
            info['do_image'] = self.index['image_to_name'].get(droplet['image_id'])
            info['do_distro'] = self.index['image_to_distro'].get(droplet['image_id'])

        return info



    ###########################################################################
    # Cache Management
    ###########################################################################
    
    
    
    
    
    
    
    
    
    

    def is_cache_valid(self):
        ''' Determines if the cache files have expired, or if it is still valid '''
        if os.path.isfile(self.cache_filename):
            mod_time = os.path.getmtime(self.cache_filename)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                return True
        return False


    def load_from_cache(self):
        ''' Reads the data from the cache file and assigns it to member variables as Python Objects'''
        cache = open(self.cache_filename, 'r')
        json_data = cache.read()
        cache.close()
        data = json.loads(json_data)

        self.data = data['data']
        self.inventory = data['inventory']
        self.index = data['index']


    def write_to_cache(self):
        ''' Writes data in JSON format to a file '''
        data = { 'data': self.data, 'index': self.index, 'inventory': self.inventory }
        json_data = json.dumps(data, sort_keys=True, indent=2)

        cache = open(self.cache_filename, 'w')
        cache.write(json_data)
        cache.close()



    ###########################################################################
    # Utilities
    ###########################################################################

    def push(self, my_dict, key, element):
        ''' Pushed an element onto an array that may not have been defined in the dict '''
        if key in my_dict:
            my_dict[key].append(element);
        else:
            my_dict[key] = [element]


    def to_safe(self, word):
        ''' Converts 'bad' characters in a string to underscores so they can be used as Ansible groups '''
        return re.sub("[^A-Za-z0-9\-\.]", "_", word)


    def sanitize_dict(self, d):
        new_dict = {}
        for k, v in d.items():
            if v != None:
                new_dict[self.to_safe(str(k))] = self.to_safe(str(v))
        return new_dict


    def sanitize_list(self, seq):
        new_seq = []
        for d in seq:
            new_seq.append(self.sanitize_dict(d))
        return new_seq




###########################################################################
# SQLAlachemy Models
###########################################################################

Base = declarative_base()

class Host(Base):
    __tablename__ = 'host'
    
    id = Column(Integer, primary_key=True)
    tags = relationship('Tag', secondary='host_tag_map')
    
    host = Column(String)
    host_name = Column(String)
    ssh_user = Column(String)
    ssh_port = Column(Integer)
    
    
    
class Tag(Base):
    __tablename__ = 'tag'
    
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('tag_group.id'))
    
    name = Column(String)
    
    
class TagGroup(Base):
    __tablename__ = 'tag_group'
    
    id = Column(Integer, primary_key=True)
    tags = relationship("Tag")
    
    name = Column(String)
    selection_type = Column(Enum('checkbox', 'select', 'multiselect', name='tag_group_types'))
    
     
class HostTagMap(Base):
    __tablename__ = 'host_tag_map'
    
    host_id = Column(Integer, ForeignKey('host.id'), primary_key=True)
    tag_id = Column(Integer, ForeignKey('tag.id'), primary_key=True)
    


###########################################################################
# Run the script
BlueAcornInventory()
