# -*- Mode: Python; test-case-name: flumotion.test.test_feedcomponent010 -*-
# vi:si:et:sw=4:sts=4:ts=4
#
# Flumotion - a streaming media server
# Copyright (C) 2004,2005,2006,2007 Fluendo, S.L. (www.fluendo.com).
# All rights reserved.

# This file may be distributed and/or modified under the terms of
# the GNU General Public License version 2 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE.GPL" in the source distribution for more information.

# Licensees having purchased or holding a valid Flumotion Advanced
# Streaming Server license may use this file in accordance with the
# Flumotion Advanced Streaming Server Commercial License Agreement.
# See "LICENSE.Flumotion" in the source distribution for more information.

# Headers in this file shall remain intact.

import gst
import gobject

import os
import time

from twisted.internet import reactor, defer

from flumotion.component import component as basecomponent
from flumotion.common import common, errors, pygobject, messages
from flumotion.common import gstreamer
from flumotion.component import feed, padmonitor
from flumotion.component.feeder import Feeder
from flumotion.component.eater import Eater

from flumotion.common.planet import moods

from flumotion.common.messages import N_
T_ = messages.gettexter('flumotion')


class FeedComponent(basecomponent.BaseComponent):
    """
    I am a base class for all Flumotion feed components.
    """

    # how often to update the UIState feeder statistics
    FEEDER_STATS_UPDATE_FREQUENCY = 12.5

    logCategory = 'feedcomponent'

    __signals__ = ('feed-ready', 'error')

    ### BaseComponent interface implementations
    def init(self):
        # add keys for eaters and feeders uiState
        self.feeders = {} # feeder feedName -> Feeder
        self.eaters = {} # eater eaterAlias -> Eater
        self.uiState.addListKey('feeders')
        self.uiState.addListKey('eaters')

        self.pipeline = None
        self.pipeline_signals = []
        self.bus_signal_id = None
        self.effects = {}
        self._feeder_probe_cl = None

        self._pad_monitors = padmonitor.PadMonitorSet(
            lambda: self.setMood(moods.happy),
            lambda: self.setMood(moods.hungry))

        self.clock_provider = None
        self._master_clock_info = None # (ip, port, basetime) if we're the 
                                       # clock master

        self._change_monitor = gstreamer.StateChangeMonitor()

        self._gotFirstNewSegment = {}

        # multifdsink's get-stats signal had critical bugs before this version
        self._get_stats_supported = (gstreamer.get_plugin_version('tcp')
                                     >= (0, 10, 11, 0))

    def do_setup(self):
        """
        Sets up component.

        Invokes the L{create_pipeline} and L{set_pipeline} vmethods,
        which subclasses can provide.
        """
        eater_config = self.config.get('eater', {})
        feeder_config = self.config.get('feed', [])
        source_config = self.config.get('source', [])

        self.debug("FeedComponent.do_setup(): eater_config %r", eater_config)
        self.debug("FeedComponent.do_setup(): feeder_config %r", feeder_config)
        self.debug("FeedComponent.do_setup(): source_config %r", source_config)
        # for upgrade of code without restarting managers
        # this will only be for components whose eater name in registry is
        # default, so no need to import registry and find eater name
        if eater_config == {} and source_config != []:
            eater_config = {'default': [(x, 'default') for x in source_config]}

        for eaterName in eater_config:
            for feedId, eaterAlias in eater_config[eaterName]:
                self.eaters[eaterAlias] = Eater(eaterAlias, eaterName)
                self.uiState.append('eaters', self.eaters[eaterAlias].uiState)
                
        for feederName in feeder_config:
            self.feeders[feederName] = Feeder(feederName)
            self.uiState.append('feeders',
                                 self.feeders[feederName].uiState)

        pipeline = self.create_pipeline()
        self.set_pipeline(pipeline)

        self.debug("FeedComponent.do_setup(): finished")

        return defer.succeed(None)

    ### FeedComponent interface for subclasses
    def create_pipeline(self):
        """
        Subclasses have to implement this method.

        @rtype: L{gst.Pipeline}
        """
        raise NotImplementedError, "subclass must implement create_pipeline"
        
    def set_pipeline(self, pipeline):
        """
        Subclasses can override me.
        They should chain up first.
        """
        if self.pipeline:
            self.cleanup()
        self.pipeline = pipeline
        self._setup_pipeline()

    def attachPadMonitorToFeeder(self, feederName):
        elementName = self.feeders[feederName].payName
        element = self.pipeline.get_by_name(payName)
        if not element:
            raise errors.ComponentError("No such feeder %s" % feederName)

        pad = element.get_pad('src')
        self._pad_monitors.attach(pad, elementName)

    ### FeedComponent methods
    def addEffect(self, effect):
        self.effects[effect.name] = effect
        effect.setComponent(self)

    def connect_feeders(self, pipeline):
        # Connect to the client-fd-removed signals on each feeder, so we 
        # can clean up properly on removal.
        for feeder in self.feeders.values():
            element = pipeline.get_by_name(feeder.elementName)
            element.connect('client-fd-removed', self.removeClientCallback)
            self.debug("Connected %r to removeClientCallback", feeder)

    def get_pipeline(self):
        return self.pipeline

    def do_pipeline_playing(self):
        """
        Invoked when the pipeline has changed the state to playing.
        The default implementation sets the component's mood to HAPPY.
        """
        self.setMood(moods.happy)

    def make_message_for_gstreamer_error(self, gerror, debug):
        """Make a flumotion error message to show to the user.

        This method may be overridden by components that have special
        knowledge about potential errors. If the component does not know
        about the error, it can chain up to this implementation, which
        will make a generic message.

        @param gerror: The GError from the error message posted on the
        GStreamer message bus.
        @type  gerror: L{gst.GError}
        @param  debug: A string with debugging information.
        @type   debug: str

        @returns A L{flumotion.common.messages.Message} to show to the
        user.
        """
        # generate a unique id
        mid = "%s-%s-%d" % (self.name, gerror.domain, gerror.code)
        m = messages.Error(T_(N_(
            "Internal GStreamer error.")),
            debug="%s\n%s: %d\n%s" % (
                gerror.message, gerror.domain, gerror.code, debug),
            id=mid, priority=40)
        return m

    def bus_message_received_cb(self, bus, message):
        t = message.type
        src = message.src

        if t == gst.MESSAGE_STATE_CHANGED:
            old, new, pending = message.parse_state_changed()
            name = src.get_name()
            if src == self.pipeline:
                self._change_monitor.state_changed(old, new)
        elif t == gst.MESSAGE_ERROR:
            gerror, debug = message.parse_error()
            self.warning('element %s error %s %s' %
                         (src.get_path_string(), gerror, debug))
            self.setMood(moods.sad)
            m = self.make_message_for_gstreamer_error(gerror, debug)
            self.state.append('messages', m)

            self._change_monitor.have_error(self.pipeline.get_state(),
                                            gerror.message)
        elif t == gst.MESSAGE_EOS:
            name = src.get_name()
            if name in self._pad_monitors:
                self.info('End of stream in element %s', name)
                self._pad_monitors[name].setInactive()
            else:
                self.info("We got an eos from %s", name)
        else:
            self.log('message received: %r' % message)

        return True

    def install_eater_continuity_watch(self, eaterWatchElements):
        """Watch a set of elements for discontinuity messages.

        @param eaterWatchElements: the set of elements to watch for
        discontinuities.
        @type eaterWatchElements: Dict of elementName => Eater.
        """
        def on_element_message(bus, message):
            src = message.src
            name = src.get_name()
            if name in eaterWatchElements:
                eater = eaterWatchElements[name]
                s = message.structure
                def timestampDiscont():
                    prevTs = s["prev-timestamp"]
                    prevDuration = s["prev-duration"]
                    curTs = s["cur-timestamp"]
                    discont = curTs - (prevTs + prevDuration)
                    dSeconds = discont / float(gst.SECOND)
                    self.debug("we have a discont on eater %s of %f s "
                               "between %s and %s ", eater.eaterAlias,
                               dSeconds, gst.TIME_ARGS(prevTs),
                               gst.TIME_ARGS(curTs))
                    eater.timestampDiscont(dSeconds,
                                           float(curTs) / float(gst.SECOND))

                def offsetDiscont():
                    prevOffsetEnd = s["prev-offset-end"]
                    curOffset = s["cur-offset"]
                    discont = curOffset - prevOffsetEnd
                    self.debug("we have a discont on eater %s of %d "
                               "units between %d and %d ",
                               eater.eaterAlias, discont, prevOffsetEnd,
                               curOffset)
                    eater.offsetDiscont(discont, curOffset)

                handlers = {'imperfect-timestamp': timestampDiscont,
                            'imperfect-offset': offsetDiscont}
                if s.get_name() in handlers:
                    handlers[s.get_name()]()

        # we know that there is a signal watch already installed
        bus = self.pipeline.get_bus()
        # never gets cleaned up; does that matter?
        bus.connect("message::element", on_element_message)
            
    def _setup_pipeline(self):
        self.debug('setup_pipeline()')
        assert self.bus_signal_id == None

        # disable the pipeline's management of base_time -- we're going
        # to set it ourselves.
        self.pipeline.set_new_stream_time(gst.CLOCK_TIME_NONE)

        self.pipeline.set_name('pipeline-' + self.getName())
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self.bus_signal_id = bus.connect('message',
            self.bus_message_received_cb)
        sig_id = self.pipeline.connect('deep-notify',
                                       gstreamer.verbose_deep_notify_cb, self)
        self.pipeline_signals.append(sig_id)

        # start checking feeders, if we have a sufficiently recent multifdsink
        if self._get_stats_supported:
            self._feeder_probe_cl = reactor.callLater(
                self.FEEDER_STATS_UPDATE_FREQUENCY, self._feeder_probe_calllater)
        else:
            self.warning("Feeder statistics unavailable, your "
                "gst-plugins-base is too old")
            m = messages.Warning(T_(N_(
                    "Your gst-plugins-base is too old, so "
                    "feeder statistics will be unavailable.")), 
                    id='multifdsink')
            m.add(T_(N_(
                "Please upgrade '%s' to version %s."), 'gst-plugins-base',
                '0.10.11'))
            self.addMessage(m)

    def pipeline_stop(self):
        if not self.pipeline:
            return
        
        if self.clock_provider:
            self.clock_provider.set_property('active', False)
            self.clock_provider = None
        retval = self.pipeline.set_state(gst.STATE_NULL)
        if retval != gst.STATE_CHANGE_SUCCESS:
            self.warning('Setting pipeline to NULL failed')

    def cleanup(self):
        self.debug("cleaning up")
        
        assert self.pipeline != None

        self.pipeline_stop()
        # Disconnect signals
        map(self.pipeline.disconnect, self.pipeline_signals)
        self.pipeline_signals = []
        if self.bus_signal_id:
            self.pipeline.get_bus().disconnect(self.bus_signal_id)
            self.pipeline.get_bus().remove_signal_watch()
            self.bus_signal_id = None
        self.pipeline = None

        if self._feeder_probe_cl:
            self._feeder_probe_cl.cancel()
            self._feeder_probe_cl = None

        # clean up checkEater callLaters
        for eater in self.eaters:
            self._pad_monitors.remove(eater.elementName)
            eater.setPadMonitor(None)

    def do_stop(self):
        self.debug('Stopping')
        if self.pipeline:
            self.cleanup()
        self.debug('Stopped')
        return defer.succeed(None)

    def set_master_clock(self, ip, port, base_time):
        self.debug("Master clock set to %s:%d with base_time %s", ip, port, 
            gst.TIME_ARGS(base_time))

        clock = gst.NetClientClock(None, ip, port, base_time)
        self.pipeline.set_base_time(base_time)
        self.pipeline.use_clock(clock)

    def get_master_clock(self):
        """
        Return the connection details for the master clock, if any.
        """

        return self._master_clock_info

    def provide_master_clock(self, port):
        """
        Tell the component to provide a master clock on the given port.

        @returns: a deferred firing a (ip, port, base_time) triple.
        """
        def pipelinePaused(r):
            clock = self.pipeline.get_clock()
            # make sure the pipeline sticks with this clock
            self.pipeline.use_clock(clock)

            self.clock_provider = gst.NetTimeProvider(clock, None, port)
            # small window here but that's ok
            self.clock_provider.set_property('active', False)
        
            base_time = clock.get_time()
            self.pipeline.set_base_time(base_time)

            self.debug('provided master clock from %r, base time %s'
                       % (clock, gst.TIME_ARGS(base_time)))

            if self.medium:
                # FIXME: This isn't always correct. We need a more flexible API,
                # and a proper network map, to do this. Even then, it's not 
                # always going to be possible.
                ip = self.medium.getIP()
            else:
                ip = "127.0.0.1"

            self._master_clock_info = (ip, port, base_time)
            return self._master_clock_info

        if not self.pipeline:
            self.warning('No self.pipeline, cannot provide master clock')
            # FIXME: should we have a NoSetupError() for cases where setup
            # was not called ? For now we fall through and get an exception

        if self.clock_provider:
            self.warning('already had a clock provider, removing it')
            self.clock_provider = None

        # We need to be >= PAUSED to get the correct clock, in general
        (ret, state, pending) = self.pipeline.get_state(0)
        if state != gst.STATE_PAUSED and state != gst.STATE_PLAYING:
            self.info ("Setting pipeline to PAUSED")

            d = self._change_monitor.add(gst.STATE_CHANGE_READY_TO_PAUSED)
            d.addCallback(pipelinePaused)

            self.pipeline.set_state(gst.STATE_PAUSED)
            return d
        else:
            self.info ("Pipeline already started, retrieving clocking")
            # Just return the already set up info, as a fired deferred
            return defer.succeed(self._master_clock_info)

    ### BaseComponent interface implementation
    def do_start(self, clocking):
        """
        Tell the component to start.
        Whatever is using the component is responsible for making sure all
        eaters have received their file descriptor to eat from.

        @param clocking: tuple of (ip, port, base_time) of a master clock,
                         or None not to slave the clock
        @type  clocking: tuple(str, int, long) or None.
        """
        self.debug('FeedComponent.start')
        if clocking:
            host, port, base_time = clocking
            self.info('slaving to master clock on %s:%d with base time %d',
                      host, port, base_time)
            self.set_master_clock(host, port, base_time)
        
        # set pipeline to playing, and provide clock if asked for
        if self.clock_provider:
            self.clock_provider.set_property('active', True)

        # attach event-probe callbacks for each eater
        for eater in self.eaters.values():
            self.debug('adding event probe for eater %s',
                       eater.eaterAlias)
            name = eater.elementName
            fdsrc = self.get_element(name)
            # FIXME: should probably raise
            if not fdsrc:
                self.warning('No element named %s in pipeline' % name)
                continue
            pad = fdsrc.get_pad("src")
            pad.add_event_probe(self._eater_event_probe_cb,
                                eater.eaterAlias)
            gdp_version = gstreamer.get_plugin_version('gdp')
            if gdp_version[2] < 11 and not (gdp_version[2] == 10 and \
                                            gdp_version[3] > 0):
                depay = self.get_element("%s-depay" % name)
                depaysrc = depay.get_pad("src")
                depaysrc.add_event_probe(self._depay_eater_event_probe_cb, 
                    eater.eaterAlias)

            self._pad_monitors.attach(pad, name,
                                      padmonitor.EaterPadMonitor,
                                      self.reconnectEater)
            eater.setPadMonitor(self._pad_monitors[name])

        self.debug("Setting pipeline %r to GST_STATE_PLAYING", self.pipeline)
        self.pipeline.set_state(gst.STATE_PLAYING)

        d = self._change_monitor.add(gst.STATE_CHANGE_PAUSED_TO_PLAYING)
        d.addCallback(lambda x: self.do_pipeline_playing())
        return d

    def _feeder_probe_calllater(self):
        for feedId, feeder in self.feeders.items():
            feederElement = self.get_element(feeder.elementName)
            for client in feeder.getClients():
                # a currently disconnected client will have fd None
                if client.fd is not None:
                    array = feederElement.emit('get-stats', client.fd)
                    if len(array) == 0:
                        # There is an unavoidable race here: we can't know 
                        # whether the fd has been removed from multifdsink.
                        # However, if we call get-stats on an fd that 
                        # multifdsink doesn't know about, we just get a 0-length
                        # array. We ensure that we don't reuse the FD too soon
                        # so this can't result in calling this on a valid but 
                        # WRONG fd
                        self.debug('Feeder element for feed %s does not know '
                            'client fd %d' % (feedId, client.fd))
                    else:
                        client.setStats(array)
        self._feeder_probe_cl = reactor.callLater(self.FEEDER_STATS_UPDATE_FREQUENCY, 
            self._feeder_probe_calllater)

    def reconnectEater(self, eaterAlias):
        if not self.medium:
            self.debug("Can't reconnect eater %s, running "
                       "without a medium", eaterAlias)
            return

        self.eaters[eaterAlias].disconnected()
        self.medium.connectEater(eaterAlias)

    def get_element(self, element_name):
        """Get an element out of the pipeline.

        If it is possible that the component has not yet been set up,
        the caller needs to check if self.pipeline is actually set.
        """
        assert self.pipeline
        self.log('Looking up element %r in pipeline %r',
                 element_name, self.pipeline)
        element = self.pipeline.get_by_name(element_name)
        if not element:
            self.warning("No element named %r in pipeline", element_name)
        return element
    
    def get_element_property(self, element_name, property):
        'Gets a property of an element in the GStreamer pipeline.'
        self.debug("%s: getting property %s of element %s" % (self.getName(), property, element_name))
        element = self.get_element(element_name)
        if not element:
            msg = "Element '%s' does not exist" % element_name
            self.warning(msg)
            raise errors.PropertyError(msg)
        
        self.debug('getting property %s on element %s' % (property, element_name))
        try:
            value = element.get_property(property)
        except (ValueError, TypeError):
            msg = "Property '%s' on element '%s' does not exist" % (property, element_name)
            self.warning(msg)
            raise errors.PropertyError(msg)

        # param enums and enums need to be returned by integer value
        if isinstance(value, gobject.GEnum):
            value = int(value)

        return value

    def set_element_property(self, element_name, property, value):
        'Sets a property on an element in the GStreamer pipeline.'
        self.debug("%s: setting property %s of element %s to %s" % (
            self.getName(), property, element_name, value))
        element = self.get_element(element_name)
        if not element:
            msg = "Element '%s' does not exist" % element_name
            self.warning(msg)
            raise errors.PropertyError(msg)

        self.debug('setting property %s on element %r to %s' %
                   (property, element_name, value))
        pygobject.gobject_set_property(element, property, value)
    
    ### methods to connect component eaters and feeders
    def feedToFD(self, feedName, fd, cleanup, eaterId=None):
        """
        @param feedName: name of the feed to feed to the given fd.
        @type  feedName: str
        @param fd:       the file descriptor to feed to
        @type  fd:       int
        @param cleanup:  the function to call when the FD is no longer feeding
        @type  cleanup:  callable
        """
        self.debug('FeedToFD(%s, %d)', feedName, fd)

        # We must have a pipeline in READY or above to do this. Do a 
        # non-blocking (zero timeout) get_state.
        if not self.pipeline or self.pipeline.get_state(0)[1] == gst.STATE_NULL:
            self.warning('told to feed %s to fd %d, but pipeline not '
                         'running yet', feedName, fd)
            cleanup(fd)
            # can happen if we are restarting but the other component is
            # happy; assume other side will reconnect later
            return

        if feedName not in self.feeders:
            msg = "Cannot find feeder named '%s'" % feedName
            mid = "feedToFD-%s" % feedName
            m = messages.Warning(T_(N_("Internal Flumotion error.")),
                debug=msg, id=mid, priority=40)
            self.state.append('messages', m)
            self.warning(msg)
            return False

        feeder = self.feeders[feedName]
        element = self.get_element(feeder.elementName)
        assert element
        clientId = eaterId or ('client-%d' % fd)
        element.emit('add', fd)
        feeder.clientConnected(clientId, fd, cleanup)

    def removeClientCallback(self, sink, fd):
        """
        Called (as a signal callback) when the FD is no longer in use by
        multifdsink.
        This will call the registered callable on the fd.

        Called from GStreamer threads.
        """
        self.debug("cleaning up fd %d", fd)
        name = sink.get_name()
        for feeder in self.feeders.values():
            if feeder.elementName == name:
                feeder.clientDisconnected(fd)
                return
        self.warning('Unknown feeder element %s', name)

    def eatFromFD(self, eaterAlias, feedId, fd):
        """
        Tell the component to eat the given feedId from the given fd.
        The component takes over the ownership of the fd, closing it when
        no longer eating.

        @param eaterAlias: the alias of the eater
        @type  eaterAlias: str
        @param feedId: feed id (componentName:feedName) to eat from through
                       the given fd
        @type  feedId: str
        @param fd:     the file descriptor to eat from
        @type  fd:     int
        """
        self.debug('EatFromFD(%s, %s, %d)', eaterAlias, feedId, fd)

        if not self.pipeline:
            self.warning('told to eat %s from fd %d, but pipeline not '
                         'running yet', feedId, fd)
            # can happen if we are restarting but the other component is
            # happy; assume other side will reconnect later
            os.close(fd)
            return

        if eaterAlias not in self.eaters:
            self.warning('Unknown eater alias: %s', eaterAlias)
            os.close(fd)
            return
        
        eater = self.eaters[eaterAlias]
        element = self.get_element(eater.elementName)
        if not element:
            self.warning('Eater element %s not found', eater.elementName)
            os.close(fd)
            return
 
        # fdsrc only switches to the new fd in ready or below
        (result, current, pending) = element.get_state(0L)
        if current not in [gst.STATE_NULL, gst.STATE_READY]:
            self.debug('eater %s in state %r, kidnapping it',
                       eaterAlias, current)

            # we unlink fdsrc from its peer, take it out of the pipeline
            # so we can set it to READY without having it send EOS,
            # then switch fd and put it back in.
            # To do this safely, we first block fdsrc:src, then let the 
            # component do any neccesary unlocking (needed for multi-input
            # elements)
            srcpad = element.get_pad('src')
            
            def _block_cb(pad, blocked):
                pass
            srcpad.set_blocked_async(True, _block_cb)
            self.unblock_eater(eaterAlias)

            # Now, we can switch FD with this mess
            sinkpad = srcpad.get_peer()
            srcpad.unlink(sinkpad)
            parent = element.get_parent()
            parent.remove(element)
            self.log("setting to ready")
            element.set_state(gst.STATE_READY)
            self.log("setting to ready complete!!!")
            old = element.get_property('fd')
            os.close(old)
            element.set_property('fd', fd)
            parent.add(element)
            srcpad.link(sinkpad)
            element.set_state(gst.STATE_PLAYING)
            # We're done; unblock the pad
            srcpad.set_blocked_async(False, _block_cb)
        else:
            element.set_property('fd', fd)

        # update our eater uiState, saying that we are eating from a
        # possibly new feedId
        eater.connected(fd, feedId)

    def unblock_eater(self, eaterAlias):
        """
        After this function returns, the stream lock for this eater must have
        been released. If your component needs to do something here, override
        this method.
        """
        pass

    def _eater_event_probe_cb(self, pad, event, eaterAlias):
        """
        An event probe used to consume unwanted EOS events on eaters.

        Called from GStreamer threads.
        """
        if event.type == gst.EVENT_EOS:    
            self.info('End of stream for eater %s, disconnect will be '
                      'triggered', eaterAlias)
            # We swallow it because otherwise our component acts on the EOS
            # and we can't recover from that later.  Instead, fdsrc will be
            # taken out and given a new fd on the next eatFromFD call.
            return False
        return True

    def _depay_eater_event_probe_cb(self, pad, event, eaterAlias):
        """
        An event probe used to consume unwanted duplicate newsegment events.

        Called from GStreamer threads.
        """
        if event.type == gst.EVENT_NEWSEGMENT:
            # We do this because we know gdppay/gdpdepay screw up on 2nd
            # newsegments
            if eaterAlias in self._gotFirstNewSegment:
                self.info("Subsequent new segment event received on "
                          "depay on eater %s", eaterAlias)
                # swallow (gulp)
                return False
            else:
                self._gotFirstNewSegment[eaterAlias] = True
        return True
