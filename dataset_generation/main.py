import json
import logging
import os
import subprocess
import csv
import requests
import threading
import time
from mininet.net import Mininet
from mininet.node import RemoteController
import sys
import importlib.util

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def import_module_from_path(module_name, path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[module_name] = module
    return module

class DatasetGenerator:
    def __init__(self, config_path):
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        self.project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '.'))
        self.stop_event = threading.Event()
        self.label_timeline = []

    def run(self):
        if os.geteuid() != 0:
            logging.error("This script requires sudo privileges. Please run with sudo.")
            return

        logging.info("Starting dataset generation process.")
        self._start_ryu_controller()
        time.sleep(5)
        self._start_mininet()

        offline_collector = threading.Thread(target=self._collect_offline_data)
        online_collector = threading.Thread(target=self._collect_online_data)

        offline_collector.start()
        online_collector.start()

        self._generate_traffic()

        self.stop_event.set()
        offline_collector.join()
        online_collector.join()

        self._stop_mininet()
        self._stop_ryu_controller()
        self._process_offline_data()
        self._write_label_timeline()
        logging.info("Dataset generation process finished.")

    def _start_ryu_controller(self):
        logging.info("Starting Ryu controller.")
        ryu_app_path = os.path.join(self.project_root, self.config['ryu_app'])
        self.ryu_process = subprocess.Popen(
            ['ryu-manager', ryu_app_path, '--ofp-tcp-listen-port', str(self.config['controller_port']), '--wsapi-port', str(self.config['api_port'])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _stop_ryu_controller(self):
        logging.info("Stopping Ryu controller.")
        self.ryu_process.terminate()
        self.ryu_process.wait()

    def _collect_online_data(self):
        logging.info("Starting online data collection.")
        output_file = os.path.join(self.project_root, self.config['online_collection']['output_file'])
        poll_interval = self.config['online_collection']['poll_interval']
        api_port = self.config['api_port']

        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'datapath_id', 'flow_id', 'ip_src', 'ip_dst', 'port_src', 'port_dst', 'ip_proto', 'packet_count', 'byte_count', 'duration_sec'])

            while not self.stop_event.is_set():
                try:
                    url = f'http://localhost:{api_port}/flows'
                    response = requests.get(url)
                    if response.status_code == 200:
                        flows = response.json()
                        timestamp = time.time()
                        for flow in flows:
                            match = flow.get('match', {})
                            writer.writerow([
                                timestamp,
                                flow.get('switch_id'),
                                flow.get('cookie'),
                                match.get('ipv4_src'),
                                match.get('ipv4_dst'),
                                match.get('tcp_src') or match.get('udp_src'),
                                match.get('tcp_dst') or match.get('udp_dst'),
                                match.get('ip_proto'),
                                flow.get('packet_count'),
                                flow.get('byte_count'),
                                flow.get('duration_sec')
                            ])
                except requests.exceptions.ConnectionError:
                    logging.warning("Could not connect to Ryu controller API. Retrying...")
                time.sleep(poll_interval)

    def _start_mininet(self):
        logging.info("Starting Mininet.")
        mininet_topology_path = os.path.join(self.project_root, self.config['mininet_topology'])
        spec = import_module_from_path("mininet_topology", mininet_topology_path)
        topology_class = getattr(spec, "CustomTopology")
        self.net = Mininet(topo=topology_class(), controller=RemoteController('c0', ip='127.0.0.1', port=self.config['controller_port']))
        self.net.start()

        self.dpid = self.net.switches[0].dpid
        logging.info(f"Captured DPID: {self.dpid}")

        self.hosts = {host.name: host for host in self.net.hosts}
        logging.info(f"Captured hosts: {list(self.hosts.keys())}")

    def _stop_mininet(self):
        logging.info("Stopping Mininet.")
        self.net.stop()

    def _generate_traffic(self):
        logging.info("Generating traffic.")
        traffic_config = self.config['traffic_types']

        # Generate normal traffic
        normal_duration = traffic_config['normal']['duration']
        normal_scapy_commands = traffic_config['normal'].get('scapy_commands', [])

        logging.info(f"Generating normal traffic for {normal_duration} seconds.")
        start_time = time.time()
        self.label_timeline.append({'start_time': start_time, 'end_time': start_time + normal_duration, 'label': 'normal'})

        normal_traffic_processes = []
        for cmd_info in normal_scapy_commands:
            host = self.hosts[cmd_info['host']]
            scapy_command = f"from scapy.all import *; {cmd_info['command']}"
            process = host.cmd(f'python3 -c "{scapy_command}" > /dev/null 2>&1 &')
            normal_traffic_processes.append(process)

        time.sleep(normal_duration)

        for process in normal_traffic_processes:
            process.send_signal(subprocess.SIGINT) # Attempt to stop Scapy processes gracefully

        # Generate attack traffic
        attacks = traffic_config.get('attacks', [])
        for attack in attacks:
            attack_type = attack['type']
            attack_duration = attack['duration']
            attacker_host = self.hosts[attack['attacker']]
            scapy_command = attack['scapy_command']

            logging.info(f"Generating {attack_type} attack from {attack['attacker']} to {attack['victim']} for {attack_duration} seconds.")
            start_time = time.time()
            self.label_timeline.append({'start_time': start_time, 'end_time': start_time + attack_duration, 'label': attack_type})

            attack_script_path = os.path.join(self.project_root, 'attacks', attack['script_name'])
            if not os.path.exists(attack_script_path):
                logging.error(f"Attack script not found: {attack_script_path}")
                continue

            try:
                attack_module = import_module_from_path(attack_type, attack_script_path)
                victim_ip = self.hosts[attack['victim']].IP()
                attack_thread = threading.Thread(target=attack_module.run_attack, args=(attacker_host, victim_ip, attack_duration))
                attack_thread.start()
                attack_thread.join() # Wait for the attack thread to complete
            except Exception as e:
                logging.error(f"Error running attack {attack_type}: {e}")

    def _collect_offline_data(self):
        logging.info("Starting offline data collection.")
        pcap_file = os.path.join(self.project_root, self.config['offline_collection']['pcap_file'])
        self.tcpdump_process = subprocess.Popen(['dumpcap', '-i', 'any', '-w', pcap_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.stop_event.wait()
        self.tcpdump_process.terminate()
        self.tcpdump_process.wait()

    def _process_offline_data(self):
        logging.info("Processing offline data.")
        pcap_file = os.path.join(self.project_root, self.config['offline_collection']['pcap_file'])
        output_file = os.path.join(self.project_root, self.config['offline_collection']['output_file'])
        cicflowmeter_path = self.config['offline_collection']['cicflowmeter_path']

        if not os.path.exists(cicflowmeter_path):
            logging.error(f"CICFlowMeter not found at {cicflowmeter_path}. Skipping offline data processing.")
            return

        subprocess.run([cicflowmeter_path, pcap_file, output_file], check=True)

    def _write_label_timeline(self):
        logging.info("Writing label timeline to CSV.")
        output_file = os.path.join(self.project_root, self.config['label_timeline_file'])
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['start_time', 'end_time', 'label'])
            for entry in self.label_timeline:
                writer.writerow([entry['start_time'], entry['end_time'], entry['label']])

if __name__ == '__main__':
    generator = DatasetGenerator('config.json')
    generator.run()