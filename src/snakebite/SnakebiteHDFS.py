#
# Make sure you install snamebite in the seclected python environment.
#
# E.g.
#      pip install snakebite
#
import snakebite.client as client
import snakebite.protobuf.ClientNamenodeProtocol_pb2 as client_proto
from snakebite.config import HDFSConfig
from snakebite.namenode import Namenode
from snakebite.client import Client
from snakebite.channel import DataXceiverChannel
import Queue


class SnakebiteHDFS(object):
    def __init__(self):
        self.client = self.create_client()

    @staticmethod
    def fields(proto):
        """Helper function to list protocol buffer fields"""
        return [x[0].name for x in proto.ListFields()]

    @staticmethod
    def create_client():
        configs = HDFSConfig.get_external_config()
        namenodes = []
        config = configs[0]
        namenode, port = config['namenode'], config['port']
        return client.Client(namenode, port=port)

    def print_blocks(self, blocks):
        print "Pool:", set(block.b.poolId for block in blocks)
        fmt = "%10s | %10s | %11s | %15s"
        print fmt % ("Bytes", "Block ID", "# Locations", "Hostnames")
        for block in blocks:
            num_bytes, block_id = str(block.b.numBytes), str(block.b.blockId)
            hostnames = [loc.id.hostName.split('.')[0] for loc in block.locs]
            print fmt % (num_bytes, block_id, len(block.locs), ','.join(hostnames))

    def find_blocks(self, path):
        client = self.client
        fileinfo = client._get_file_info(path)
        node = fileinfo.fs
        length = node.length
        request = client_proto.GetBlockLocationsRequestProto()
        request.src = path
        request.length = length
        request.offset = 0L
        response = client.service.getBlockLocations(request)
        return list(response.locations.blocks)

   
    @staticmethod
    def get_locations(blocks):
        return set(loc.id.ipAddr for block in blocks for loc in block.locs)


    @staticmethod
    def read_block(block):
        location = block.locs[0]
        host = location.id.ipAddr
        port = int(location.id.xferPort)
        data_xciever = DataXceiverChannel(host, port)
        if not data_xciever.connect():
            return
        offset_in_block = 0
        check_crc = False
        length = block.b.numBytes
        block_gen = data_xciever.readBlock(length, block.b.poolId, block.b.blockId, block.b.generationStamp, offset_in_block, check_crc)
        return block_gen


    def find_out_things(self, path, tail_only=False, check_crc=False):
        client = self.client
        fileinfo = client._get_file_info(path)
        node = fileinfo.fs
        length = node.length
        print "Length: ", length

        request = client_proto.GetBlockLocationsRequestProto()
        request.src = path
        request.length = length

        if tail_only:  # Only read last KB
            request.offset = max(0, length - 1024)
        else:
            request.offset = 0L
        response = client.service.getBlockLocations(request)

        lastblock = response.locations.lastBlock

        #if tail_only:
        #    if lastblock.b.blockId == response.locations.blocks[0].b.blockId:
        #        num_blocks_tail = 1  # Tail is on last block
        #    else:
        #        num_blocks_tail = 2  # Tail is on two blocks

        failed_nodes = []
        total_bytes_read = 0
        for block in response.locations.blocks:
            length = block.b.numBytes
            pool_id = block.b.poolId
            print "Length: %s, pool_id: %s" % (length, pool_id)
            offset_in_block = 0
            if tail_only:
                if num_blocks_tail == 2 and block.b.blockId != lastblock.b.blockId:
                    offset_in_block = block.b.numBytes - (1024 - lastblock.b.numBytes)
                elif num_blocks_tail == 1:
                    offset_in_block = max(0, lastblock.b.numBytes - 1024)
            # Prioritize locations to read from
            locations_queue = Queue.PriorityQueue()  # Primitive queuing based on a node's past failure
            for location in block.locs:
                if location.id.storageID in failed_nodes:
                    locations_queue.put((1, location))  # Priority num, data
                else:
                    locations_queue.put((0, location))

            # Read data
            successful_read = False
            while not locations_queue.empty():
                location = locations_queue.get()[1]
                host = location.id.ipAddr
                port = int(location.id.xferPort)
                data_xciever = DataXceiverChannel(host, port)
                if data_xciever.connect():
                    try:
                        for load in data_xciever.readBlock(length, pool_id, block.b.blockId, block.b.generationStamp, offset_in_block, check_crc):
                            offset_in_block += len(load)
                            total_bytes_read += len(load)
                            successful_read = True
                            yield load
                    except Exception, e:
                        log.error(e)
                        if not location.id.storageID in failed_nodes:
                            failed_nodes.append(location.id.storageID)
                        successful_read = False
                else:
                    raise Exception
                if successful_read:
                    break
            if successful_read is False:
                raise Exception("Failure to read block %s" % block.b.blockId)

