#!/usr/bin/python
#	vim:fileencoding=utf-8
# Monitor current builds and display their logs on split-screen basis
# (C) 2010 Michał Górny, distributed under the terms of the 3-clause BSD license

MY_PV='0.2.2'

import portage

import pyinotify
import curses, locale, re

import optparse
import sys, os, codecs
import time, fcntl, errno, glob

def check_lock(path):
	try:
		lockf = open(path, 'r+')
	except IOError:
		return False

	try:
		fcntl.lockf(lockf.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
	except IOError as e:
		lockf.close()
		if e.errno == errno.EACCES or e.errno == errno.EAGAIN:
			return True
		else:
			raise
	else: # file not locked? probably stale
		fcntl.lockf(lockf.fileno(), fcntl.LOCK_UN)
		lockf.close()
	return False

class Screen:
	def __init__(self, root, firstpdir, opts):
		curses.use_default_colors()

		self.root = root
		self.sbar = None
		self.windows = []
		self.inactive = []
		self.colors = {(-1, -1): 0}
		self.escregex = re.compile(r'(\x1b\[(?:\d*;)*\d*[a-zA-Z@`]|\07)')
		self.firstpdir = firstpdir
		self.opts = opts
		if sys.version_info[0] < 3:
			self.encode = codecs.getencoder(locale.nl_langinfo(locale.CODESET))
		else:
			self.encode = lambda x,y: (x,)
		self.redraw()

	def addwin(self, win, basedir, lockfn = None):
		win.win = None
		win.nwin = None
		win.basedir = basedir
		win.lockfn = lockfn
		win.backlog = ''
		win.activity = time.time()
		win.lockcheck = win.activity
		win.expectclose = 0
		self.windows.append(win)
		self.redraw()

	def delwin(self, win):
		del win.win
		del win.nwin
		self.windows.remove(win)
		if win in self.inactive:
			self.inactive.remove(win)
		self.redraw()

	def findwin(self, basedir):
		for w in self.windows:
			if w.basedir == basedir:
				return w

		return None

	def redraw(self):
		(height, width) = self.root.getmaxyx()
		# get so much backlog so we could fill in the whole window
		# if a merge becomes the only one left
		self.backloglen = (height - 2) * width
		jobcount = len(self.windows)

		del self.sbar
		mergecount = 0
		for w in self.windows:
			del w.win
			del w.nwin
			if not w.basedir.startswith('_'):
				mergecount += 1

		self.root.clear()
		self.root.noutrefresh()

		self.sbar = curses.newwin(1, width, height - 1, 0)
		self.sbar.addstr(0, 0, 'portage-jobsmon.py', curses.A_BOLD)
		if mergecount == 0:
			self.sbar.addstr(' (waiting for some merge to start)')
		else:
			self.sbar.addstr(' (monitoring ')
			if mergecount == 1:
				self.sbar.addstr('single', curses.A_BOLD)
				self.sbar.addstr(' merge process)')
			else:
				self.sbar.addstr(str(mergecount), curses.A_BOLD)
				self.sbar.addstr(' parallel merges)')
		self.sbar.noutrefresh()

		jobcount -= len(self.inactive)

		if jobcount > 0:
			jobrows = int((height - 1) / jobcount)
			jobrowsleft = (height - 1) % jobcount
			if jobrows < 4:
				jobrows = 4
				jobcount = int((height - 1) / jobrows)
				jobrowsleft = (height - 1) % jobcount
			if jobrowsleft > 0:
				jobrowsleft += 1
				jobrows += 1

			starty = 0
			for w in self.windows:
				if w not in self.inactive and jobcount > 0:
					if jobrowsleft > 0:
						jobrowsleft -= 1
						if jobrowsleft == 0:
							jobrows -= 1

					w.win = curses.newwin(jobrows - 1, width, starty, 0)
					w.win.idlok(1)
					w.win.scrollok(1)

					w.newline = False
					w.win.move(0, 0)
					self.append(w, w.backlog, True)

					starty += jobrows
					w.nwin = curses.newwin(1, width, starty - 1, 0)
					w.nwin.bkgd(' ', curses.A_REVERSE)
					if w.basedir == '_fetch':
						w.nwin.addstr(0, 0, '(parallel fetch)')
					else:
						dir = [self.encode(x, 'replace')[0] for x in w.basedir.rsplit('/', 2)]
						w.nwin.addstr(0, 0, '[%s]' % '/'.join(dir[1:3]))
						if dir[0] != self.firstpdir:
							w.nwin.addstr(' (in %s)' % dir[0], curses.A_DIM)
					w.nwin.noutrefresh()

					jobcount -= 1
				else: # job won't fit on the screen
					w.win = None
					w.nwin = None
		elif len(self.inactive) > 0:
			for w in self.inactive:
				w.win = None
				w.nwin = None

		curses.doupdate()

	def getcolor(self, fg, bg):
		if (fg, bg) not in self.colors.keys():
			n = len(self.colors)
			if n >= curses.COLOR_PAIRS:
				if self.opts.debug:
					raise Exception('COLOR_PAIRS (%d) exceeded, no more space for (%d, %d)' % (curses.COLOR_PAIRS, fg, bg))
				return curses.color_pair(0)
			curses.init_pair(n, fg, bg)
			self.colors[(fg, bg)] = n

		return curses.color_pair(self.colors[(fg, bg)])

	def append(self, w, text, omitbacklog = False):
		w.activity = time.time()
		if w in self.inactive:
			self.inactive.remove(w)
			self.redraw()

		if not omitbacklog:
			bl = w.backlog + text
			w.backlog = bl[-self.backloglen:]

		if w.win is not None:
			# delay newlines to avoid wasting vertical space
			if w.newline:
				w.win.addstr('\n')
			w.newline = text.endswith('\n')
			if w.newline:
				text = text[:-1]

			# parse ECMA-48 CSI
			mode = 0
			fgcol = -1
			bgcol = -1
			ptext = self.escregex.split(text)
			for i in range(len(ptext)):
				if i % 2 == 0:
					w.win.addstr(self.encode(ptext[i], 'replace')[0])
				elif ptext[i][0] == '\07':
					if self.opts.vabell or not self.opts.vbell:
						curses.beep()
					if self.opts.vbell:
						curses.flash()
				else: # \e[
					x = curses
					attrmapping = [None, x.A_BOLD, x.A_DIM, None,
							x.A_UNDERLINE, x.A_BLINK, None, x.A_REVERSE]
					colrmapping = [x.COLOR_BLACK, x.COLOR_RED, x.COLOR_GREEN, x.COLOR_YELLOW,
							x.COLOR_BLUE, x.COLOR_MAGENTA, x.COLOR_CYAN, x.COLOR_WHITE]

					func = ptext[i][-1]
					args = ptext[i][2:-1].split(';')
					if func == 'm': # SGR
						for a in args:
							if a != '':
								a = int(a, 10)
							else:
								a = 0

							if a == 0:
								mode = 0
								fgcol = -1
								bgcol = -1
							elif a in [1,2,4,5,7]:
								mode |= attrmapping[a]
							elif a in [21,22]:
								mode &= ~(attrmapping[1] | attrmapping[2])
							elif a in [24,25,27]:
								mode &= ~attrmapping[a % 20]
							elif a >= 30 and a <= 37:
								fgcol = colrmapping[a % 30]
							elif a == 38:
								mode |= curses.A_UNDERLINE
								fgcol = -1
							elif a == 39:
								mode &= ~curses.A_UNDERLINE
								fgcol = -1
							elif a >= 40 and a <= 47:
								bgcol = colrmapping[a % 40]
							elif a == 49:
								bgcol = -1
							elif self.opts.debug:
								raise Exception('Unsupported SGR %d' % a)
						w.win.attrset(mode | self.getcolor(fgcol, bgcol))
					elif ord(func) in range(ord('A'), ord('H')): # cursor-related
						if func != 'H':
							(y, x) = w.win.getyx()
						max = w.win.getmaxyx()
						if args[0] != '':
							arg = int(args[0], 10)
						else:
							arg = 1

						if func in ['A', 'F']:
							y -= arg
						elif func in ['B', 'E']:
							y += arg
						elif func == 'C':
							x += arg
						elif func == 'D':
							x -= arg
						elif func == 'G':
							x = arg - 1
						elif func == 'H':
							y = arg - 1
							if args[1] != '':
								x = int(args[1]) - 1
							else:
								x = 0
						if func in ['E', 'F']:
							x = 1

						# sanity checks
						if y < 0:
							y = 0
						elif y >= max[0]: # XXX: scrolling?
							y = max[0] - 1
						if x < 0:
							x = 0
						elif x >= max[1]: # XXX: wrapping?
							x = max[1] - 1

						w.win.move(y, x)
					elif self.opts.debug:
						raise Exception('Unsupported func %s (args: %s)' % (func, args))

			w.win.refresh()

	def checkact(self, ts, pullinterval, acttimeout, lockcheckint):
		redraw = False
		winrem = []

		for w in self.windows:
			if pullinterval != 0 and ts - w.pullts >= pullinterval:
				data = w.pull()
				if data is not None:
					self.append(w, data)

			if acttimeout != 0 and w not in self.inactive and ts - w.activity >= acttimeout:
				self.inactive.append(w)
				redraw = True

			if w.lockfn is not None and (acttimeout == 0 or w in self.inactive) and ts - w.lockcheck >= lockcheckint:
				w.expectclose += 1
				if not check_lock(w.lockfn):
					winrem.append(w)
				else:
					w.lockcheck = time.time()

		for w in winrem:
			self.delwin(w)

		if redraw:
			self.redraw()

class FileTailer:
	def __init__(self, fn, scr = None):
		self.fn = fn
		self.file = None
		self.pullts = time.time()
		self.scr = scr

		self.reopen()

	def __del__(self):
		if self.file is not None:
			self.file.close()

	def reopen(self):
		if self.file is not None:
			self.file.close()

		# Well, in fact we could open a file as normal bytestream and pass the data
		# to ncurses without decoding and then encoding it back but we would then
		# pass broken characters too (e.g. due to seeking). And we wouldn't support
		# running portage-jobsmon on terminal with different character encoding
		# than one used in ebuilds (utf-8).

		self.file = codecs.open(self.fn, 'rb', 'utf-8', 'ignore')
		if self.scr is not None:
			try:
				self.file.seek(-self.scr.backloglen, os.SEEK_END)
			except IOError:
				pass # seek() can fail

	def pull(self):
		self.pullts = time.time()
		data = self.file.read()
		if len(data) == 0:
			data = None
		return data

def cursesmain(cscr, opts, args):
	if opts.tempdir is None:
		tempdir = [portage.settings['PORTAGE_TMPDIR']]
	else:
		tempdir = opts.tempdir
	pdir = ['%s/portage' % x for x in tempdir]
	firstpdir = pdir[0] # the default one
	pdir.sort(key=len, reverse=True)
	scr = Screen(cscr, firstpdir, opts)

	def ppath(dir):
		for pd in pdir:
			if not dir.startswith(pd):
				continue
			dir = dir[len(pd)+1:].split('/')
			dir.insert(0, pd)
			return dir
		return None

	def pfilter(dir):
		if dir in tempdir:
			return False
		dir = ppath(dir)
		if dir is None:
			return True
		elif len(dir) == 4:
			if dir[3] != 'temp':
				return True
		elif len(dir) > 4:
			return True

		return False

	wm = pyinotify.WatchManager()
	lockfindts = [0]

	def window_add(dir):
		basedir = '/'.join(dir[0:3])
		fn = '/'.join(dir)

		w = scr.findwin(basedir)
		if w is None:
			try:
				w = FileTailer(fn, scr)
			except IOError:
				return None

			lockfn = '%s/%s/.%s.portage_lockfile' % tuple(dir[0:3])
			scr.addwin(w, basedir, lockfn)
			wm.add_watch(lockfn, pyinotify.IN_CLOSE_WRITE)
		else:
			wm.del_watch(w.watchd)
			w.reopen()
		w.watchd = list(wm.add_watch(fn, pyinotify.IN_MODIFY).values())[0]
		return w

	def find_locks(ts):
		for d in pdir:
			fl = []
			for f in glob.glob('%s/*/.*.portage_lockfile' % d):
				try:
					mtime = os.stat(f).st_mtime
				except OSError:
					mtime = 0
				fl.append((mtime, f))
			fl.sort(reverse = True)
			for mtime, f in fl:
				assert(f.startswith(d))
				assert(f.endswith('.portage_lockfile'))
				dir = f[len(d)+1:-17].split('/.', 1)
				dir.insert(0, d)
				if scr.findwin('/'.join(dir)) is None and check_lock(f):
					dir.extend(['temp', 'build.log'])
					w = window_add(dir)
					if w is not None:
						data = w.pull()
						if data is not None:
							scr.append(w, data)
		lockfindts[0] = ts

	class Inotifier(pyinotify.ProcessEvent):
		fetchlog = '/var/log/emerge-fetch.log'

		def process_IN_CREATE(self, ev):
			if not ev.dir:
				dir = ppath(ev.pathname)
				if dir is not None:
					if len(dir) == 5 and dir[3] == 'temp' and dir[4] == 'build.log':
						window_add(dir)
						lockfindts[0] = time.time()

		def process_IN_MODIFY(self, ev):
			if ev.pathname == self.fetchlog:
				w = scr.findwin('_fetch')
				if w is None:
					w = FileTailer(self.fetchlog, scr)
					scr.addwin(w, '_fetch')
			else:
				dir = ppath(ev.pathname)
				if dir is None:
					return
				basedir = '/'.join(dir[0:3])
				w = scr.findwin(basedir)

			if w is not None:
				data = w.pull()
				if data is not None:
					scr.append(w, data)

		def process_IN_CLOSE_WRITE(self, ev):
			if ev.pathname == self.fetchlog:
				basedir = '_fetch'
			else:
				dir = ppath(ev.pathname)
				if dir is None:
					return
				basedir = '%s/%s/%s' % (dir[0], dir[1], dir[2][1:-17])

			w = scr.findwin(basedir)
			if w is not None:
				if w.expectclose > 0:
					w.expectclose -= 1
				else:
					scr.delwin(w)
					del w

	def timeriter(sth):
		ts = time.time()
		scr.checkact(ts, opts.pullint, opts.inact, opts.lockcheck)
		if opts.lockfind != 0 and ts - lockfindts[0] > opts.lockfind:
			find_locks(ts)

	np = Inotifier()
	n = pyinotify.Notifier(wm, np, timeout = opts.timeout * 1000)
	if not opts.omitrunning:
		find_locks(time.time())
	for t in tempdir:
		wm.add_watch(t, pyinotify.IN_CREATE,
				rec=True, auto_add=True, exclude_filter=pfilter)
	if opts.watchfetch:
		wm.add_watch(np.fetchlog, pyinotify.IN_MODIFY | pyinotify.IN_CLOSE_WRITE)
	n.loop(callback = timeriter)

def main(argv):
	parser = optparse.OptionParser(
			version = '%%prog %s' % MY_PV,
			description = 'Monitor parallel emerge builds and display logs on a split-screen basis.'
		)
	parser.add_option('-D', '--debug', action='store_true', dest='debug', default=False,
			help='Enable unsupported action debugging (raises exceptions when unsupported escape sequence is found)')
	parser.add_option('-F', '--ignore-fetchlog', action='store_false', dest='watchfetch', default=True,
			help='Omit monitoring /var/log/emerge-fetch.log for parallel fetch progress')
	parser.add_option('-o', '--omit-running', action='store_true', dest='omitrunning', default=False,
			help='Omit catching all running emerges during startup, watch only those started after the program')
	parser.add_option('-t', '--tempdir', action='append', dest='tempdir',
			help="Temporary directory to watch (without the 'portage/' suffix); if specified multiple times, all specified directories will be watched; if not specified, defaults to ${PORTAGE_TEMPDIR}")

	og = optparse.OptionGroup(parser, 'Appearance')
	og.add_option('-v', '--visual-bell', action='store_true', dest='vbell', default=False,
			help='Use visual bell (flash the screen) instead of audible one')
	og.add_option('-V', '--visual-and-audible-bell', action='store_true', dest='vabell', default=False,
			help='Use both visual and audible bell')
	parser.add_option_group(og)

	og = optparse.OptionGroup(parser, 'Fine-tuning')
	og.add_option('-A', '--inactivity-timeout', action='store', dest='inact', type='float', default=30,
			help='Timeout after which inactive emerge process will be shifted off the screen (def: 30 s)')
	og.add_option('-l', '--lock-check-interval', action='store', dest='lockcheck', type='float', default=15,
			help='Interval between lockfile checks on inactive (or active if inactivity timeout disabled) windows (def: 15 s)')
	og.add_option('-n', '--newmerge-check-interval', action='store', dest='lockfind', type='float', default=45,
			help="Interval of scanning the temporary directories for new merges if inotify doesn't notice such (def: 45 s)")
	og.add_option('-p', '--pull-interval', action='store', dest='pullint', type='float', default=10,
			help="Max interval between two consecutive pulls; forces pulling if inotify didn't notice any I/O (def: 10 s)")
	og.add_option('-T', '--timeout', action='store', dest='timeout', type='float', default=2,
			help='The timeout of the poll() call, and thus the max time between consecutive timer loop calls (def: 2 s)')
	parser.add_option_group(og)

	(opts, args) = parser.parse_args(args = argv[1:])
	opts.vbell |= opts.vabell

	locale.setlocale(locale.LC_ALL, '')
	try:
		curses.wrapper(cursesmain, opts, args)
	except KeyboardInterrupt:
		pass

if __name__ == "__main__":
	sys.exit(main(sys.argv))
