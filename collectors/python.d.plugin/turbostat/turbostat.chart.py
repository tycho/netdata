# -*- coding: utf-8 -*-
# Description: turbostat netdata python.d module
# Author: Steven Noonan (tycho)

import copy
import os
import struct
import subprocess
import time

from bases.FrameworkServices.SimpleService import SimpleService

_BASE10_LINES = set(['CPU', 'core', 'IRQ', 'SMI', 'package'])

CHART_TEMPLATES = {
    'power': {
        'options': [None, 'Power utilization', 'Watts', 'turbostat', 'turbostat', 'line'],
        '_divisor': 100,
        '_per': 'package',
        '_replace': [ ('_watts', '') ],
        '_metrics': ['pkg_watts', 'ram_watts', 'gfx_watts'],
        'lines': [
            # lines are created dynamically in `check()` method
        ]},
    'avg_mhz': {
        'options': [None, 'Average CPU clock frequency, including idle time', 'MHz', 'turbostat', 'turbostat', 'line'],
        'lines': [
            # lines are created dynamically in `check()` method
        ]},
    'busy_mhz': {
        'options': [None, 'Average CPU clock frequency, when busy', 'MHz', 'turbostat', 'turbostat', 'line'],
        'lines': [
            # lines are created dynamically in `check()` method
        ]},
}

DEVNULL = open(os.devnull, 'wb')

def read_file_line(path):
    return open(path, 'rt').read().strip()

def get_llc_dirname():
    for dirpath, dirnames, filenames in os.walk('/sys/devices/system/cpu/cpu0/cache'):
        dirnames = list(filter(lambda x: x.startswith('index'), dirnames))
        dirnames.sort()
        return dirnames[-1]

def get_rapl_power_unit(cpu):
    proc = subprocess.Popen(['turbostat', '-c', str(cpu), '-d', '0'], stdout=DEVNULL, stderr=subprocess.PIPE)
    for line in proc.stderr:
        line = line.decode('utf-8').rstrip()

        if not line:
            continue

        line = line.split()

        if line[1] == 'MSR_RAPL_POWER_UNIT:' or line[1] == 'MSR_RAPL_PWR_UNIT:':
            return int(line[2], 16)

    raise IOError("Couldn't find RAPL power unit")

class Service(SimpleService):
    def __init__(self, configuration=None, name=None):
        SimpleService.__init__(self, configuration=configuration, name=name)
        self.order = []
        self.definitions = {}
        self.fake_name = "cpu"
        self.assignment = {}
        self.last_turbostat = None
        self.last_turbostat_time = 0
        self.rapl = {}

    def _parse_stat_line(self, line):
        line = line.split(b': ', 1)

        # Line has no delimiter, probably a debug message of some kind. Ignore it.
        if len(line) == 1:
            return None

        name, value = line[0].decode('utf-8'), line[1]

        if name == 'CPU':
            return name, int(value.split()[0])

        try:
            if name in _BASE10_LINES:
                value = int(value, 10)
            else:
                value = int(value, 16)
        except:
            return None

        return name, value

    def _invoke_turbostat(self):
        cpus = {}
        proc = subprocess.Popen(['turbostat', '--Dump'], stdout=subprocess.PIPE, stderr=DEVNULL)
        cpu = None
        now = time.time()
        for line in proc.stdout:
            line = line.rstrip()

            if not line:
                cpu = None
                continue

            stat = self._parse_stat_line(line)
            if stat is None:
                #self.alert("Failed to parse line: '%s'" % (line,))
                continue

            name, value = stat

            if cpu is None and name != 'CPU':
                continue

            if name == 'CPU':
                cpu = {}
                cpus[value] = cpu

            cpu[name] = value

        return now, cpus

    def _dict_delta(self, before, after):
        result = {}
        for key, value in before.items():
            try:
                result[key] = after[key] - value
            except TypeError:
                # Probably not a numeric type. Don't worry about it.
                pass

        return result

    def _get_data(self):
        data = {}

        now, turbostat = self._invoke_turbostat()

        for cpuidx, stats in turbostat.items():
            last_stats = self.last_turbostat[cpuidx]

            delta = self._dict_delta(last_stats, stats)

            tsc = delta['TSC']
            aperf = delta['aperf']
            mperf = delta['mperf']

            time = now - self.last_turbostat_time
            avg_mhz = aperf / time / 1e3
            busy_mhz = (tsc / 1e3) * aperf / mperf / time

            cpuname = 'cpu%d' % (cpuidx,)
            data[cpuname + '_avg_mhz'] = avg_mhz
            data[cpuname + '_busy_mhz'] = busy_mhz

            pkgidx = self.assignment[cpuname]['package']
            pkgname = 'pkg%d' % (pkgidx,)

            if pkgname + '_ram_watts' not in data and pkgidx in self.rapl:
                power_units, energy_units = self.rapl[pkgidx]

                ram = max(0, delta['Joules RAM'])
                pkg = max(0, delta['Joules PKG'])
                gfx = max(0, delta['Joules GFX'])

                data[pkgname + '_ram_watts'] = ram * energy_units / time * 100.0
                data[pkgname + '_pkg_watts'] = pkg * energy_units / time * 100.0
                data[pkgname + '_gfx_watts'] = gfx * energy_units / time * 100.0

        self.last_turbostat_time = now
        self.last_turbostat = turbostat

        return data

    def check(self):
        split_by = None
        try:
            split_by = str(self.configuration['split_by'])
            if split_by not in ['logical', 'llc', 'core', 'package']:
                self.error("Value '%s' for 'split_by' configuration option isn't a valid choice, ignoring" % (split_by,))
                split_by = None
        except (KeyError, TypeError):
            self.alert("No 'split_by' option specified. Not dividing CPUs up by topology.")

        try:
            self.last_turbostat_time, self.last_turbostat = self._invoke_turbostat()
        except:
            self.error("Could not invoke turbostat, disabling.")
            return False

        llc_dirname = get_llc_dirname()

        for cpuidx, stats in self.last_turbostat.items():
            cpuname = 'cpu%d' % (cpuidx,)
            pkgidx = stats['package']
            self.assignment[cpuname] = {
                'cpuidx': cpuidx,
                'package': pkgidx,
                'core': stats['core'],
                'llc_id': int(read_file_line('/sys/devices/system/cpu/cpu%d/cache/%s/id' % (cpuidx, llc_dirname)))
            }

            if pkgidx not in self.rapl:
                try:
                    msr = get_rapl_power_unit(cpuidx)
                except IOError:
                    # We don't have CAP_SYS_RAWIO and can't rdmsr :(
                    continue
                power_units = 1.0 / (1 << (msr & 0xF))
                energy_units = 1.0 / (1 << ((msr >> 8) & 0x1F));
                self.rapl[pkgidx] = (power_units, energy_units)

        if len(self.assignment) == 0:
            self.error("Could not find any CPUs in turbostat dump")
            return False

        order = []

        packages_seen = []

        for chart, template in CHART_TEMPLATES.items():
            for cpuname in sorted(self.assignment, key=lambda v: int(v[3:].split('_')[0])):
                assignment = self.assignment[cpuname]

                cpuidx = assignment['cpuidx']
                pkgidx = assignment['package']
                pkgname = 'pkg%d' % (pkgidx,)
                coreidx = assignment['core']
                corename = 'core%d' % (coreidx,)
                llcname = 'llc%d' % (assignment['llc_id'])

                if split_by == 'package':
                    suffix = pkgname
                elif split_by == 'llc':
                    suffix = pkgname + '_' + llcname
                elif split_by == 'core':
                    suffix = pkgname + '_' + corename
                elif split_by == 'logical':
                    suffix = cpuname
                else:
                    suffix = None

                metrics = template.get('_metrics', [chart])
                per = template.get('_per', 'logical')
                divisor = template.get('_divisor', 1000)
                substitutions = template.get('_replace', [])

                if per == 'package':
                    suffix = pkgname

                chartname = chart
                if suffix is not None:
                    chartname += '_' + suffix

                if chartname not in self.definitions:
                    self.definitions[chartname] = copy.deepcopy(template)
                    order.append((chartname, (pkgidx, coreidx, cpuidx)))

                prefix = cpuname
                nickname_prefix = ''

                if per == 'package':
                    if pkgidx in packages_seen:
                        continue

                    prefix = pkgname
                    if suffix is None:
                        nickname_prefix = pkgname + '_'

                for metric in metrics:
                    nickname = cpuname
                    if per == 'package':
                        nickname = metric
                    for term, replace in substitutions:
                        nickname = nickname.replace(term, replace)
                    self.definitions[chartname]['lines'].append([prefix + '_' + metric, nickname_prefix + nickname, 'absolute', 1, divisor])

                if per == 'package':
                    packages_seen.append(pkgidx)


        self.order = [name for name, topology in sorted(order, key=lambda v: v[1])]

        return True
