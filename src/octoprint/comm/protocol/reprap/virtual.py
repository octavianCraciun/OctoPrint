# coding=utf-8
from __future__ import absolute_import
__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'


import time
import os
import re
import threading
import math
import Queue

from serial import SerialTimeoutException

class VirtualPrinter(object):
	command_regex = re.compile("^([GMTF])(\d+)")
	sleep_regex = re.compile("sleep (\d+)")
	sleep_after_regex = re.compile("sleep_after ([GMTF]\d+) (\d+)")
	sleep_after_next_regex = re.compile("sleep_after_next ([GMTF]\d+) (\d+)")
	custom_action_regex = re.compile("action_custom ([a-zA-Z0-9_]+)(\s+.*)?")

	def __init__(self, seriallog_handler=None, read_timeout=5.0, write_timeout=10.0, rx_buffer=64, command_buffer=4, extruders=1,
	             speeds=dict(x=6000, y=6000, z=300, e=200), wait=None, ok_before=False, support_m112=True, support_f=True,
	             echo_m117=True, virtual_sd=None, throttle=0.1, ok_with_lineno=False, force_checksums=False, repetier_resends=False):
		import logging
		self._logger = logging.getLogger("octoprint.plugin.virtual_printer.VirtualPrinter")

		self._seriallog = logging.getLogger("octoprint.plugin.virtual_printer.VirtualPrinter.serial")
		self._seriallog.setLevel(logging.CRITICAL)
		self._seriallog.propagate = False

		if seriallog_handler is not None:
			import logging.handlers
			self._seriallog.addHandler(seriallog_handler)
			self._seriallog.setLevel(logging.INFO)

		self._seriallog.info("-"*78)

		self._read_timeout = read_timeout
		self._write_timeout = write_timeout
		self._throttle = throttle
		self._ok_with_lineno = ok_with_lineno

		self._force_checksums = force_checksums
		self._repetier_resends = repetier_resends
		self._extruders = extruders

		self.incoming = CharCountingQueue(rx_buffer, name="RxBuffer")
		self.outgoing = Queue.Queue()
		self.buffered = Queue.Queue(maxsize=command_buffer)

		for item in ['start', 'Marlin: Virtual Marlin!', '\x80', 'SD card ok']:
			self._send(item)

		self.currentExtruder = 0
		self.temp = [0.0] * extruders
		self.targetTemp = [0.0] * extruders
		self.lastTempAt = time.time()
		self.bedTemp = 1.0
		self.bedTargetTemp = 1.0
		self.speeds = speeds

		self._relative = True
		self._lastX = None
		self._lastY = None
		self._lastZ = None
		self._lastE = None

		self._unitModifier = 1

		self._virtualSd = virtual_sd
		self._sdCardReady = True
		self._sdPrinter = None
		self._sdPrintingSemaphore = threading.Event()
		self._selectedSdFile = None
		self._selectedSdFileSize = None
		self._selectedSdFilePos = None
		self._writingToSd = False
		self._newSdFilePos = None
		self._heatupThread = None

		self._okBeforeCommandOutput = ok_before
		self._supportM112 = support_m112
		self._supportF = support_f

		self._sendWait = bool(wait)
		self._waitInterval = wait if wait else 1.0

		self._echoOnM117 = echo_m117

		self.currentLine = 0
		self.lastN = 0

		self._incoming_lock = threading.RLock()

		self._sleepAfterNext = dict()
		self._sleepAfter = dict()

		self._dont_answer = False

		self._debug_drop_connection = False

		#self._action_hooks = plugin_manager().get_hooks("octoprint.plugin.virtual_printer.custom_action")

		self._killed = False

		readThread = threading.Thread(target=self._processIncoming)
		readThread.start()

		bufferThread = threading.Thread(target=self._processBuffer)
		bufferThread.start()

	def __str__(self):
		return "VIRTUAL(read_timeout={read_timeout},write_timeout={write_timeout},options={options})"\
			.format(read_timeout=self._read_timeout, write_timeout=self._write_timeout, options="...")

	def _clearQueue(self, queue):
		try:
			while queue.get(block=False):
				continue
		except Queue.Empty:
			pass

	def _processIncoming(self):
		next_wait_timeout = time.time() + self._waitInterval
		while self.incoming is not None and not self._killed:
			self._simulateTemps()

			try:
				data = self.incoming.get(timeout=0.01)
			except Queue.Empty:
				if self._sendWait and time.time() > next_wait_timeout:
					self._send("wait")
					next_wait_timeout = time.time() + self._waitInterval
				continue

			next_wait_timeout = time.time() + self._waitInterval

			if data is None:
				continue

			if self._dont_answer:
				self._dont_answer = False
				continue

			data = data.strip()

			# strip checksum
			if "*" in data:
				data = data[:data.rfind("*")]
				self.currentLine += 1
			elif self._force_checksums:
				self._send("Error: Missing checksum")
				continue

			# track N = N + 1
			if data.startswith("N") and "M110" in data:
				linenumber = int(re.search("N([0-9]+)", data).group(1))
				self.lastN = linenumber
				self.currentLine = linenumber
				self._sendOk()
				continue
			elif data.startswith("N"):
				linenumber = int(re.search("N([0-9]+)", data).group(1))
				expected = self.lastN + 1
				if linenumber != expected:
					self._triggerResend(actual=linenumber)
					continue
				elif self.currentLine == 101:
					# simulate a resend at line 100
					self._triggerResend(expected=100)
					continue
				else:
					self.lastN = linenumber
				data = data.split(None, 1)[1].strip()

			data += "\n"

			# shortcut for writing to SD
			if self._writingToSd and not self._selectedSdFile is None and not "M29" in data:
				with open(self._selectedSdFile, "a") as f:
					f.write(data)
				self._sendOk()
				continue

			if data.strip() == "version":
				from octoprint._version import get_versions
				self._send("OctoPrint VirtualPrinter v" + get_versions()["version"])
				continue
			elif data.startswith("!!DEBUG:") or data.strip() == "!!DEBUG":
				debug_command = ""
				if data.startswith("!!DEBUG:"):
					debug_command = data[len("!!DEBUG:"):].strip()
				self._debugTrigger(debug_command)
				continue

			# if we are sending oks before command output, send it now
			if len(data.strip()) > 0 and self._okBeforeCommandOutput:
				self._sendOk()

			# actual command handling
			command_match = VirtualPrinter.command_regex.match(data)
			if command_match is not None:
				command = command_match.group(0)
				letter = command_match.group(1)

				try:
					# if we have a method _gcode_G, _gcode_M or _gcode_T, execute that first
					letter_handler = "_gcode_{}".format(letter)
					if hasattr(self, letter_handler):
						code = command_match.group(2)
						handled = getattr(self, letter_handler)(code, data)
						if handled:
							continue

					# then look for a method _gcode_<command> and execute that if it exists
					command_handler = "_gcode_{}".format(command)
					if hasattr(self, command_handler):
						handled = getattr(self, command_handler)(data)
						if handled:
							continue

				finally:
					# make sure that the debug sleepAfter and sleepAfterNext stuff works even
					# if we continued above
					if len(self._sleepAfter) or len(self._sleepAfterNext):
						interval = None
						if command in self._sleepAfter:
							interval = self._sleepAfter[command]
						elif command in self._sleepAfterNext:
							interval = self._sleepAfterNext[command]
							del self._sleepAfterNext[command]

						if interval is not None:
							self._send("// sleeping for {interval} seconds".format(interval=interval))
							time.sleep(interval)

			# if we are sending oks after command output, send it now
			if len(data.strip()) > 0 and not self._okBeforeCommandOutput:
				self._sendOk()

	##~~ command implementations

	def _gcode_T(self, code, data):
		self.currentExtruder = int(code)
		self._send("Active Extruder: %d" % self.currentExtruder)

	def _gcode_F(self, code, data):
		if self._supportF:
			self._send("echo:changed F value")
			return False
		else:
			self._send("Error: Unknown command F")
			return True

	def _gcode_M104(self, data):
		self._parseHotendCommand(data)
	_gcode_M109 = _gcode_M104

	def _gcode_M140(self, data):
		self._parseBedCommand(data)
	_gcode_M190 = _gcode_M140

	def _gcode_M105(self, data):
		self._processTemperatureQuery()
		return True

	def _gcode_M20(self, data):
		if self._sdCardReady:
			self._listSd()

	def _gcode_M21(self, data):
		self._sdCardReady = True
		self._send("SD card ok")

	def _gcode_M22(self, data):
		self._sdCardReady = False

	def _gcode_M23(self, data):
		if self._sdCardReady:
			filename = data.split(None, 1)[1].strip()
			self._selectSdFile(filename)

	def _gcode_M24(self, data):
		if self._sdCardReady:
			self._startSdPrint()

	def _gcode_M25(self, data):
		if self._sdCardReady:
			self._pauseSdPrint()

	def _gcode_M26(self, data):
		if self._sdCardReady:
			pos = int(re.search("S([0-9]+)", data).group(1))
			self._setSdPos(pos)

	def _gcode_M27(self, data):
		if self._sdCardReady:
			self._reportSdStatus()

	def _gcode_M28(self, data):
		if self._sdCardReady:
			filename = data.split(None, 1)[1].strip()
			self._writeSdFile(filename)

	def _gcode_M29(self, data):
		if self._sdCardReady:
			self._finishSdFile()

	def _gcode_M30(self, data):
		if self._sdCardReady:
			filename = data.split(None, 1)[1].strip()
			self._deleteSdFile(filename)

	def _gcode_M114(self, data):
		output = "C: X:{} Y:{} Z:{} E:{}".format(self._lastX, self._lastY, self._lastZ, self._lastE)
		if not self._okBeforeCommandOutput:
			output = "ok " + output
		self._send(output)
		return True

	def _gcode_M117(self, data):
		# we'll just use this to echo a message, to allow playing around with pause triggers
		if self._echoOnM117:
			self._send("echo:%s" % re.search("M117\s+(.*)", data).group(1))

	def _gcode_M400(self, data):
		self.buffered.join()

	def _gcode_M999(self, data):
		# mirror Marlin behaviour
		self._send("Resend: 1")

	def _gcode_G20(self, data):
		self._unitModifier = 1.0 / 2.54
		if self._lastX is not None:
			self._lastX *= 2.54
		if self._lastY is not None:
			self._lastY *= 2.54
		if self._lastZ is not None:
			self._lastZ *= 2.54
		if self._lastE is not None:
			self._lastE *= 2.54

	def _gcode_G21(self, data):
		self._unitModifier = 1.0
		if self._lastX is not None:
			self._lastX /= 2.54
		if self._lastY is not None:
			self._lastY /= 2.54
		if self._lastZ is not None:
			self._lastZ /= 2.54
		if self._lastE is not None:
			self._lastE /= 2.54

	def _gcode_G90(self, data):
		self._relative = False

	def _gcode_G91(self, data):
		self._relative = True

	def _gcode_G92(self, data):
		self._setPosition(data)

	def _gcode_G28(self, data):
		self._performMove(data)

	def _gcode_G0(self, data):
		# simulate reprap buffered commands via a Queue with maxsize which internally simulates the moves
		self.buffered.put(data)
	_gcode_G1 = _gcode_G0
	_gcode_G2 = _gcode_G0
	_gcode_G3 = _gcode_G0

	##~~ further helpers

	def _kill(self):
		if not self._supportM112:
			return
		self._killed = True
		self._send("echo:EMERGENCY SHUTDOWN DETECTED. KILLED.")

	def _triggerResend(self, expected=None, actual=None):
		with self._incoming_lock:
			if expected is None:
				expected = self.lastN + 1
			else:
				self.lastN = expected - 1

			if actual is None:
				self._send("Error: Wrong checksum")
			else:
				self._send("Error: expected line %d got %d" % (expected, actual))

			def request_resend():
				self._send("Resend:%d" % expected)
				self._send("ok")

			if self._repetier_resends:
				request_resend()
			request_resend()

	def _debugTrigger(self, data):
		if data == "" or data == "help" or data == "?":
			usage = """
			OctoPrint Virtual Printer debug commands

			help
			?
			| This help.

			# Action Triggers

			action_pause
			| Sends a "// action:pause" action trigger to the host.
			action_resume
			| Sends a "// action:resume" action trigger to the host.
			action_disconnect
			| Sends a "// action:disconnect" action trigger to the
			| host.
			action_custom <action>[ <parameters>]
			| Sends a custom "// action:<action> <parameters>"
			| action trigger to the host.

			# Communication Errors

			dont_answer
			| Will not acknowledge the next command.
			trigger_resend_lineno
			| Triggers a resend error with a line number mismatch
			trigger_resend_checksum
			| Triggers a resend error with a checksum mismatch
			drop_connection
			| Drops the serial connection

			# Reply Timing / Sleeping

			sleep <int:seconds>
			| Sleep <seconds> s
			sleep_after <str:command> <int:seconds>
			| Sleeps <seconds> s after each execution of <command>
			sleep_after_next <str:command> <int:seconds>
			| Sleeps <seconds> s after execution of <command>
			"""
			for line in usage.split("\n"):
				self._send("echo: {}".format(line.strip()))
		elif data == "action_pause":
			self._send("// action:pause")
		elif data == "action_resume":
			self._send("// action:resume")
		elif data == "action_disconnect":
			self._send("// action:disconnect")
		elif data == "dont_answer":
			self._dont_answer = True
		elif data == "trigger_resend_lineno":
			self._triggerResend(expected=self.lastN, actual=self.lastN+1)
		elif data == "trigger_resend_checksum":
			self._triggerResend(expected=self.lastN)
		elif data == "drop_connection":
			self._debug_drop_connection = True
		else:
			try:
				sleep_match = VirtualPrinter.sleep_regex.match(data)
				sleep_after_match = VirtualPrinter.sleep_after_regex.match(data)
				sleep_after_next_match = VirtualPrinter.sleep_after_next_regex.match(data)
				custom_action_match = VirtualPrinter.custom_action_regex.match(data)

				if sleep_match is not None:
					interval = int(sleep_match.group(1))
					self._send("// sleeping for {interval} seconds".format(interval=interval))
					time.sleep(interval)
				elif sleep_after_match is not None:
					command = sleep_after_match.group(1)
					interval = int(sleep_after_match.group(2))
					self._sleepAfter[command] = interval
					self._send("// going to sleep {interval} seconds after each {command}".format(**locals()))
				elif sleep_after_next_match is not None:
					command = sleep_after_next_match.group(1)
					interval = int(sleep_after_next_match.group(2))
					self._sleepAfterNext[command] = interval
					self._send("// going to sleep {interval} seconds after next {command}".format(**locals()))
				elif custom_action_match is not None:
					action = custom_action_match.group(1)
					params = custom_action_match.group(2)
					params = params.strip() if params is not None else ""
					self._send("// action:{action} {params}".format(**locals()).strip())
			except:
				pass

	def _listSd(self):
		self._send("Begin file list")
		items = map(
			lambda x: "%s %d" % (x.upper(), os.stat(os.path.join(self._virtualSd, x)).st_size),
			os.listdir(self._virtualSd)
		)
		for item in items:
			self._send(item)
		self._send("End file list")

	def _selectSdFile(self, filename):
		if filename.startswith("/"):
			filename = filename[1:]
		file = os.path.join(self._virtualSd, filename.lower())
		if not os.path.exists(file) or not os.path.isfile(file):
			self._send("open failed, File: %s." % filename)
		else:
			self._selectedSdFile = file
			self._selectedSdFileSize = os.stat(file).st_size
			self._send("File opened: %s  Size: %d" % (filename, self._selectedSdFileSize))
			self._send("File selected")

	def _startSdPrint(self):
		if self._selectedSdFile is not None:
			if self._sdPrinter is None:
				self._sdPrinter = threading.Thread(target=self._sdPrintingWorker)
				self._sdPrinter.start()
		self._sdPrintingSemaphore.set()

	def _pauseSdPrint(self):
		self._sdPrintingSemaphore.clear()

	def _setSdPos(self, pos):
		self._newSdFilePos = pos

	def _reportSdStatus(self):
		if self._sdPrinter is not None and self._sdPrintingSemaphore.is_set:
			self._send("SD printing byte %d/%d" % (self._selectedSdFilePos, self._selectedSdFileSize))
		else:
			self._send("Not SD printing")

	def _processTemperatureQuery(self):
		includeTarget = True
		includeOk = not self._okBeforeCommandOutput

		# send simulated temperature data
		if self._extruders > 1:
			allTemps = []
			for i in range(len(self.temp)):
				allTemps.append((i, self.temp[i], self.targetTemp[i]))
			allTempsString = " ".join(map(lambda x: "T%d:%.2f /%.2f" % x if includeTarget else "T%d:%.2f" % (x[0], x[1]), allTemps))

			if includeTarget:
				allTempsString = "B:%.2f /%.2f %s" % (self.bedTemp, self.bedTargetTemp, allTempsString)
			else:
				allTempsString = "B:%.2f %s" % (self.bedTemp, allTempsString)

			output = "%s @:64\n" % allTempsString
		else:
			output = "T:%.2f /%.2f B:%.2f /%.2f @:64\n" % (self.temp[0], self.targetTemp[0], self.bedTemp, self.bedTargetTemp)

		if includeOk:
			output = "ok " + output
		self._send(output)

	def _parseHotendCommand(self, line):
		tool = 0
		toolMatch = re.search('T([0-9]+)', line)
		if toolMatch:
			try:
				tool = int(toolMatch.group(1))
			except:
				pass

		if tool >= self._extruders:
			return

		try:
			self.targetTemp[tool] = float(re.search('S([0-9]+)', line).group(1))
		except:
			pass

		if "M109" in line:
			self._waitForHeatup("tool%d" % tool)

	def _parseBedCommand(self, line):
		try:
			self.bedTargetTemp = float(re.search('S([0-9]+)', line).group(1))
		except:
			pass

		if "M190" in line:
			self._waitForHeatup("bed")

	def _performMove(self, line):
		matchX = re.search("X([0-9.]+)", line)
		matchY = re.search("Y([0-9.]+)", line)
		matchZ = re.search("Z([0-9.]+)", line)
		matchE = re.search("E([0-9.]+)", line)

		duration = 0
		if matchX is not None:
			try:
				x = float(matchX.group(1))
				if self._relative or self._lastX is None:
					duration = max(duration, x * self._unitModifier / float(self.speeds["x"]) * 60.0)
				else:
					duration = max(duration, (x - self._lastX) * self._unitModifier / float(self.speeds["x"]) * 60.0)
				self._lastX = x
			except:
				pass
		if matchY is not None:
			try:
				y = float(matchY.group(1))
				if self._relative or self._lastY is None:
					duration = max(duration, y * self._unitModifier / float(self.speeds["y"]) * 60.0)
				else:
					duration = max(duration, (y - self._lastY) * self._unitModifier / float(self.speeds["y"]) * 60.0)
				self._lastY = y
			except:
				pass
		if matchZ is not None:
			try:
				z = float(matchZ.group(1))
				if self._relative or self._lastZ is None:
					duration = max(duration, z * self._unitModifier / float(self.speeds["z"]) * 60.0)
				else:
					duration = max(duration, (z - self._lastZ) * self._unitModifier / float(self.speeds["z"]) * 60.0)
				self._lastZ = z
			except:
				pass
		if matchE is not None:
			try:
				e = float(matchE.group(1))
				if self._relative or self._lastE is None:
					duration = max(duration, e * self._unitModifier / float(self.speeds["e"]) * 60.0)
				else:
					duration = max(duration, (e - self._lastE) * self._unitModifier / float(self.speeds["e"]) * 60.0)
				self._lastE = e
			except:
				pass

		if duration:
			slept = 0
			while duration - slept > self._read_timeout and not self._killed:
				time.sleep(self._read_timeout)
				slept += self._read_timeout

	def _setPosition(self, line):
		matchX = re.search("X([0-9.]+)", line)
		matchY = re.search("Y([0-9.]+)", line)
		matchZ = re.search("Z([0-9.]+)", line)
		matchE = re.search("E([0-9.]+)", line)

		if matchX is None and matchY is None and matchZ is None and matchE is None:
			self._lastX = self._lastY = self._lastZ = self._lastE = 0
		else:
			if matchX is not None:
				try:
					self._lastX = float(matchX.group(1))
				except:
					pass
			if matchY is not None:
				try:
					self._lastY = float(matchY.group(1))
				except:
					pass
			if matchZ is not None:
				try:
					self._lastZ = float(matchZ.group(1))
				except:
					pass
			if matchE is not None:
				try:
					self._lastE = float(matchE.group(1))
				except:
					pass

	def _writeSdFile(self, filename):
		if filename.startswith("/"):
			filename = filename[1:]
		file = os.path.join(self._virtualSd, filename).lower()
		if os.path.exists(file):
			if os.path.isfile(file):
				os.remove(file)
			else:
				self._send("error writing to file")

		self._writingToSd = True
		self._selectedSdFile = file
		self._send("Writing to file: %s" % filename)

	def _finishSdFile(self):
		self._writingToSd = False
		self._selectedSdFile = None

	def _sdPrintingWorker(self):
		self._selectedSdFilePos = 0
		with open(self._selectedSdFile, "r") as f:
			for line in iter(f.readline, ""):
				if self._killed:
					break

				# reset position if requested by client
				if self._newSdFilePos is not None:
					f.seek(self._newSdFilePos)
					self._newSdFilePos = None

				# read current file position
				self._selectedSdFilePos = f.tell()

				# if we are paused, wait for unpausing
				self._sdPrintingSemaphore.wait()

				# set target temps
				if 'M104' in line or 'M109' in line:
					self._parseHotendCommand(line)
				if 'M140' in line or 'M190' in line:
					self._parseBedCommand(line)

				time.sleep(0.1)

		self._sdPrintingSemaphore.clear()
		self._selectedSdFilePos = 0
		self._sdPrinter = None
		self._send("Done printing file")

	def _waitForHeatup(self, heater):
		delta = 1
		delay = 1
		if heater.startswith("tool"):
			toolNum = int(heater[len("tool"):])
			while not self._killed and (self.temp[toolNum] < self.targetTemp[toolNum] - delta or self.temp[toolNum] > self.targetTemp[toolNum] + delta):
				self._simulateTemps(delta=delta)
				self._send("T:%0.2f" % self.temp[toolNum])
				time.sleep(delay)
		elif heater == "bed":
			while not self._killed and (self.bedTemp < self.bedTargetTemp - delta or self.bedTemp > self.bedTargetTemp + delta):
				self._simulateTemps(delta=delta)
				self._send("B:%0.2f" % self.bedTemp)
				time.sleep(delay)

	def _deleteSdFile(self, filename):
		if filename.startswith("/"):
			filename = filename[1:]
		f = os.path.join(self._virtualSd, filename)
		if os.path.exists(f) and os.path.isfile(f):
			os.remove(f)

	def _simulateTemps(self, delta=1):
		timeDiff = self.lastTempAt - time.time()
		self.lastTempAt = time.time()
		for i in range(len(self.temp)):
			if abs(self.temp[i] - self.targetTemp[i]) > delta:
				oldVal = self.temp[i]
				self.temp[i] += math.copysign(timeDiff * 10, self.targetTemp[i] - self.temp[i])
				if math.copysign(1, self.targetTemp[i] - oldVal) != math.copysign(1, self.targetTemp[i] - self.temp[i]):
					self.temp[i] = self.targetTemp[i]
				if self.temp[i] < 0:
					self.temp[i] = 0
		if abs(self.bedTemp - self.bedTargetTemp) > delta:
			oldVal = self.bedTemp
			self.bedTemp += math.copysign(timeDiff * 10, self.bedTargetTemp - self.bedTemp)
			if math.copysign(1, self.bedTargetTemp - oldVal) != math.copysign(1, self.bedTargetTemp - self.bedTemp):
				self.bedTemp = self.bedTargetTemp
			if self.bedTemp < 0:
				self.bedTemp = 0

	def _processBuffer(self):
		while self.buffered is not None:
			try:
				line = self.buffered.get(timeout=0.5)
			except Queue.Empty:
				continue

			if line is None:
				continue

			self._performMove(line)
			self.buffered.task_done()

	def write(self, data):
		if self._debug_drop_connection:
			self._logger.info("Debug drop of connection requested, raising SerialTimeoutException")
			raise SerialTimeoutException()

		with self._incoming_lock:
			if self.incoming is None or self.outgoing is None:
				return

			if "M112" in data and self._supportM112:
				self._seriallog.info("<<< {}".format(data.strip()))
				self._kill()
				return

			try:
				self.incoming.put(data, timeout=self._write_timeout)
				self._seriallog.info("<<< {}".format(data.strip()))
			except Queue.Full:
				self._logger.info("Incoming queue is full, raising SerialTimeoutException")
				raise SerialTimeoutException()

	def read(self, size=None):
		if self._debug_drop_connection:
			raise SerialTimeoutException()

		try:
			line = self.outgoing.get(timeout=self._read_timeout) + "\n"
			time.sleep(self._throttle)
			self._seriallog.info(">>> {}".format(line.strip()))
			return line
		except Queue.Empty:
			return ""

	def readline(self):
		return self.read()

	def close(self):
		self.incoming = None
		self.outgoing = None
		self.buffered = None

	def _sendOk(self):
		if self.outgoing is None:
			return

		if self._ok_with_lineno:
			self._send("ok %d" % self.lastN)
		else:
			self._send("ok")

	def _sendWaitAfterTimeout(self, timeout=5):
		time.sleep(timeout)
		if self.outgoing is not None:
			self._send("wait")

	def _send(self, line):
		if self.outgoing is not None:
			self.outgoing.put(line)

class CharCountingQueue(Queue.Queue):

	def __init__(self, maxsize, name=None):
		Queue.Queue.__init__(self, maxsize=maxsize)
		self._size = 0
		self._name = name

	def put(self, item, block=True, timeout=None):
		self.not_full.acquire()
		try:
			item_size = self._len(item)

			if not block:
				if self._qsize() + item_size >= self.maxsize:
					raise Queue.Full
			elif timeout is None:
				while self._qsize() + item_size >= self.maxsize:
					self.not_full.wait()
			elif timeout < 0:
				raise ValueError("'timeout' must be a positive number")
			else:
				endtime = time.time() + timeout
				while self._qsize() + item_size >= self.maxsize:
					remaining = endtime - time.time()
					if remaining <= 0.0:
						raise Queue.Full
					self.not_full.wait(remaining)

			self._put(item)
			self.unfinished_tasks += 1
			self.not_empty.notify()
		finally:
			self.not_full.release()

	def _len(self, item):
		return len(item)

	def _qsize(self, len=len):
		return self._size

	# Put a new item in the queue
	def _put(self, item):
		self.queue.append(item)
		self._size += self._len(item)

	# Get an item from the queue
	def _get(self):
		item = self.queue.popleft()
		self._size -= self._len(item)
		return item