# -*- Mode: Python -*-
# vi:si:et:sw=4:sts=4:ts=4

# Flumotion - a streaming media server
# Copyright (C) 2004,2005,2006,2007,2008,2009 Fluendo, S.L.
# Copyright (C) 2010,2011 Flumotion Services, S.A.
# All rights reserved.
#
# This file may be distributed and/or modified under the terms of
# the GNU Lesser General Public License version 2.1 as published by
# the Free Software Foundation.
# This file is distributed without any warranty; without even the implied
# warranty of merchantability or fitness for a particular purpose.
# See "LICENSE.LGPL" in the source distribution for more information.
#
# Headers in this file shall remain intact.

from flumotion.component.plugs import base

__version__ = "$Rev$"


class StreamDataProviderPlug(base.ComponentPlug):
    """
    Base class for streamdata plugs, a plug that allows a streamer's
    getStreamData() method to be pluggable.
    """

    def getStreamData(self):
        """Get the stream data for a streamer.

        This interface is useful if you want to use an external
        component to create a playlist for a streamer. The returned data
        structure would then be used in making that playlist. Flumotion
        core does not currently include such a component, however.
        """
        raise NotImplementedError


class StreamDataProviderExamplePlug(StreamDataProviderPlug):
    description = None
    url = None

    def start(self, component):
        self.url = component.getUrl()
        self.description = self.args['properties']['description']

    def getStreamData(self):
        return {'protocol': 'HTTP',
                'description': self.description,
                'url': self.url}