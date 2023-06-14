#!/usr/bin/python3
import argparse
import json
import queue
import shutil
import signal
import socket
import sys
import threading
import time
import traceback

import docker
import redis


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


label_name = "hoster.domains"
enclosing_pattern = "#-----------Docker-Hoster-Domains----------\n"
hosts_path = "/tmp/hosts"
hosts = {}
redis_client = None
local_ip = get_local_ip()
local_host = socket.getfqdn(socket.gethostname())
redis_channel = "hoster"
preferred_networks = []


def signal_handler(sig, frame):
    global hosts
    hosts = {}
    update_hosts_file()
    sys.exit(0)


def listen_pub(p, q):
    for item in p.listen():
        if item['type'] == 'message':
            all_hosts = {}
            for key in redis_client.scan_iter("hoster:*"):
                all_hosts[key[7:]] = json.loads(redis_client.get(key))
            q.put(all_hosts)


def is_preferred_ip(ip):
    for n in preferred_networks:
        if ip.startswith(n):
            return True
    return False


def handle_queue(q):
    while True:
        try:
            all_hosts = q.get(False)
            hoster_hosts = {}
            for host, item in all_hosts.items():
                is_local = host == local_host
                for cid, addresses in item["hosts"].items():
                    full_cid = cid if is_local else f"{host}-{cid}"
                    hoster_hosts[full_cid] = []
                    for addr in addresses:
                        addr_name = addr["name"]
                        if not is_local:
                            if not is_preferred_ip(addr["ip"]):
                                continue
                            if "docker-hoster" in addr_name:
                                continue
                        addr_domains = []
                        if is_local:
                            addr_domains.append(addr_name)
                            addr_domains.append(cid)
                        addr_domains.append(f"{addr_name}.{host}")
                        new_addr = {
                            "ip": addr["ip"],
                            "name": addr_name,
                            "domains": addr_domains
                        }
                        hoster_hosts[full_cid].append(new_addr)

            do_update_hosts_file(hoster_hosts)
        except queue.Empty:
            time.sleep(2)
        except Exception:
            print(traceback.format_exc())


def listen_docker(docker_client):
    events = docker_client.events(decode=True)
    # listen for events to keep the hosts file updated
    for e in events:
        if e["Type"] != "container":
            continue

        status = e["status"]
        if status == "start":
            container_id = e["id"]
            container = get_container_data(docker_client, container_id)
            hosts[container_id] = container
            update_hosts_file()

        if status == "stop" or status == "die" or status == "destroy":
            container_id = e["id"]
            if container_id in hosts:
                hosts.pop(container_id)
                update_hosts_file()


def main():
    # register the exit signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    args = parse_args()
    global hosts_path
    hosts_path = args.file

    global preferred_networks
    preferred_networks = args.preferred_networks.split(',')

    global redis_client
    redis_client = redis.StrictRedis(host=args.redis_host, port=args.redis_port, db=args.redis_db,
                                     decode_responses=True)
    redis_pubsub = redis_client.pubsub()

    global redis_channel
    redis_channel = args.redis_channel
    redis_pubsub.subscribe(args.redis_channel)

    q = queue.Queue()

    docker_client = docker.APIClient(base_url='unix://%s' % args.socket)
    # get running containers
    for c in docker_client.containers(quiet=True, all=False):
        container_id = c["Id"]
        container = get_container_data(docker_client, container_id)
        hosts[container_id] = container

    update_hosts_file()

    threads = [
        threading.Thread(target=listen_pub, args=(redis_pubsub, q)),
        threading.Thread(target=handle_queue, args=(q,)),
        threading.Thread(target=listen_docker, args=(docker_client,))]

    for i in threads:
        i.start()

    for i in threads:
        i.join()


def get_container_data(docker_client, container_id):
    # extract all the info with the docker api
    info = docker_client.inspect_container(container_id)
    container_hostname = info["Config"]["Hostname"]
    container_name = info["Name"].strip("/")
    container_ip = info["NetworkSettings"]["IPAddress"]
    if info["Config"]["Domainname"]:
        container_hostname = container_hostname + "." + info["Config"]["Domainname"]

    result = []

    for values in info["NetworkSettings"]["Networks"].values():

        if not values["Aliases"]:
            continue

        result.append({
            "ip": values["IPAddress"],
            "name": container_name,
            "domains": list(values["Aliases"] + [container_name, container_hostname])
        })

    if container_ip:
        result.append({"ip": container_ip, "name": container_name, "domains": [container_name, container_hostname]})

    return result


def update_hosts_file():
    item = {"ip": local_ip, "host": local_host, "hosts": hosts}
    redis_client.set("hoster:" + local_host, json.dumps(item))
    redis_client.publish(redis_channel, local_ip)


def do_update_hosts_file(hoster_hosts):
    if len(hoster_hosts) == 0:
        print("Removing all hosts before exit...")
    else:
        print("Updating hosts file with:")

    for cid, addresses in hoster_hosts.items():
        for addr in addresses:
            print("ip: %s domains: %s" % (addr["ip"], addr["domains"]))

    # read all the lines of the original file
    lines = []
    with open(hosts_path, "r+") as hosts_file:
        lines = hosts_file.readlines()

    # remove all the lines after the known pattern
    for i, line in enumerate(lines):
        if line == enclosing_pattern:
            lines = lines[:i]
            break

    # remove all the trailing newlines on the line list
    if lines:
        while lines[-1].strip() == "": lines.pop()

    # append all the domain lines
    if len(hoster_hosts) > 0:
        lines.append("\n\n" + enclosing_pattern)

        for cid, addresses in hoster_hosts.items():
            for addr in addresses:
                lines.append("%s    %s\n" % (addr["ip"], "   ".join(addr["domains"])))

        lines.append("#-----Do-not-add-hosts-after-this-line-----\n\n")

    # write it on the auxiliary file
    aux_file_path = hosts_path + ".aux"
    with open(aux_file_path, "w") as aux_hosts:
        aux_hosts.writelines(lines)

    # replace etc/hosts with aux file, making it atomic
    shutil.move(aux_file_path, hosts_path)


def parse_args():
    parser = argparse.ArgumentParser(description='Synchronize running docker container IPs with host /etc/hosts file.')
    parser.add_argument('--socket', type=str, nargs="?", default="tmp/docker.sock",
                        help='The docker socket to listen for docker events.')
    parser.add_argument('--file', type=str, nargs="?", default="/tmp/hosts",
                        help='The /etc/hosts file to sync the containers with.')

    parser.add_argument('--redis_host', type=str, nargs="?", default="127.0.0.1", help='redis host')
    parser.add_argument('--redis_port', type=int, nargs="?", default=6379, help='redis port')
    parser.add_argument('--redis_db', type=int, nargs="?", default=1, help='redis db')
    parser.add_argument('--redis_channel', type=str, nargs="?", default="hoster", help='redis channel')
    parser.add_argument('--preferred_networks', type=str, nargs="?", default="10.", help='preferred networks')
    return parser.parse_args()


if __name__ == '__main__':
    main()
