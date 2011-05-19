# -*- coding: utf-8 -*-
# Copyright (c) 2011 Plivo Team. See LICENSE for details.

from gevent import monkey
monkey.patch_all()

import urllib
import urllib2

import gevent
from gevent import pool

from plivo.core.freeswitch.inboundsocket import InboundEventSocket
from plivo.rest.freeswitch.helpers import HTTPRequest


class RESTInboundSocket(InboundEventSocket):
    """
    Interface between REST API and the InboundSocket
    ...
    ...
    """
    def __init__(self, host, port, password, 
                 outbound_address='', 
                 auth_id='', auth_token='', 
                 filter="ALL", log=None):
        InboundEventSocket.__init__(self, host, port, password, filter)
        self.fs_outbound_address = outbound_address
        self.log = log
        self.auth_id = auth_id
        self.auth_token = auth_token
        # Mapping of Key: job-uuid - Value: request_id
        self.bk_jobs = {}
        # Transfer jobs: call_uuid - Value: where to transfer
        self.xfer_jobs = {}
        # Mapping of Key to-callerid vs request id to indicate ringing
        self.ring_map = {}
        # Track When Calls rang
        self.calls_ring_complete = {}
        # Call Requests Key: request_uuid - Value
        # 0 - originate_str, 1 - tonumber, 2 - gw_try_number, 3 - gw_list,
        # 4 - gw_codec_list, 5 - gw_timeout_list, 6 - gw_retry_list,
        # 7 - answer_url, 8 - hangup_url, 9 - ring_url
        self.call_request = {}

    def on_background_job(self, ev):
        """
        Capture Job Event
        Capture background job only for originate,
        and ignore all other jobs
        """
        job_uuid = ev['Job-UUID']
        job_cmd = ev['Job-Command']
        if job_cmd == "originate":
            status, info = ev.get_body().split()
            request_uuid = self.bk_jobs.pop(job_uuid, None)
            # Handle failiure case of originate - USER_NOT_REGISTERED
            # This case does not raise a on_channel_hangup event.
            # All other failiures will be captured by on_channel_hangup
            if status != '+OK':
                if info == 'USER_NOT_REGISTERED' and request_uuid:
                    #TODO: Need to check if there are some other edge cases
                    request_params = self.call_request[request_uuid]
                    hangup_url = request_params[8]
                    self.log.debug("Request: %s cannot be completed as %s"
                                                    % (request_uuid, info))
                    params = {'request_uuid': request_uuid, 'reason': info}
                    gevent.spawn(self.post_to_url, hangup_url, params)

    def on_channel_hangup(self, ev):
        """
        Capture Channel Hangup
        """
        request_uuid = ev['variable_plivo_request_uuid']
        call_uuid = ev['Unique-ID']
        reason = ev['Hangup-Cause']
        request_params = self.call_request[request_uuid]
        hangup_url = request_params[8]
        if reason == 'NORMAL_CLEARING':
            self.hangup_complete(request_uuid, call_uuid, reason, ev,
                                                                hangup_url)
        else:
            try:
                call_rang = self.calls_ring_complete[call_uuid]
            except LookupError:
                call_rang = False
            gw_list = request_params[3]
            if gw_list and not call_rang:
                self.log.debug("Originate Failed - Retrying")
                self.spawn_originate(request_uuid)
            else:
                self.hangup_complete(request_uuid, call_uuid, reason, ev,
                                                                hangup_url)

    def on_channel_state(self, ev):
        if ev['Channel-State'] == 'CS_RESET':
            call_uuid = ev['Unique-ID']
            xfer = self.xfer_jobs.pop(call_uuid, None)
            if not xfer:
                return
            self.log.info("Executing Live Call Transfer for %s" % call_uuid)
            res = self.api("uuid_transfer %s '%s' inline" % (call_uuid, xfer))
            if res.is_success():
                self.log.info("Executing Live Call Transfer Done for %s" % call_uuid)
            else:
                self.log.info("Executing Live Call Transfer Failed for %s: %s" \
                               % (call_uuid, res.get_response()))
        elif ev['Channel-State'] == 'CS_HANGUP':
            call_uuid = ev['Unique-ID']
            self.xfer_jobs.pop(call_uuid, None)
        elif ev['Answer-State'] == 'ringing' and \
            ev['Call-Direction'] == 'outbound':
            call_uuid = ev['Unique-ID']
            to = ev['Caller-Destination-Number']
            caller_id = ev['Caller-Caller-ID-Number']
            try:
                call_state = self.calls_ring_complete[call_uuid]
            except KeyError:
                call_state = False
            if not call_state:
                if to:
                    self.calls_ring_complete[call_uuid] = True
                    keystring = "%s-%s" % (to, caller_id)
                    request_uuid = self.ring_map.pop(keystring, None)
                    if request_uuid:
                        request_params = self.call_request[request_uuid]
                        ring_url = request_params[9]
                        self.log.info( \
                        "Call Ringing for: %s  with request id %s"
                                                        % (to, request_uuid))
                        params = {'to': to, 'request_uuid': request_uuid}
                        gevent.spawn(self.post_to_url, ring_url, params)

    def hangup_complete(self, request_uuid, call_uuid, reason, ev, hangup_url):
        self.log.debug("Call: %s hungup, Reason %s, Request uuid %s"
                                        % (call_uuid, reason, request_uuid))
        try:
            del self.call_request[request_uuid]
        except KeyError:
            pass
        # Check if call cleans up if no user
        try:
            del self.calls_ring_complete[call_uuid]
        except KeyError:
            pass
        self.log.debug("Call Cleaned up")
        if hangup_url:
            params = {'request_uuid': request_uuid, 'call_uuid': call_uuid,
                                                            'reason': reason}
            self.post_to_url(hangup_url, params)

    def post_to_url(self, url=None, params={}, method='POST'):
        if not url:
            return None
        http_obj = HTTPRequest(self.auth_id, self.auth_token)
        try:
            data = http_obj.fetch_response(url, params, method)
            self.log.info("Posted to %s with %s -- Result: %s"
                                            % (url, params, data))
            return data
        except Exception, e:
            self.log.error("Post to %s with %s -- Error: %s"
                                            % (url, params, e))
        return None

    def spawn_originate(self, request_uuid):
        request_params = self.call_request[request_uuid]
        originate_str = request_params[0]
        to = request_params[1]
        gw_tries_done = request_params[2]
        gw_list = request_params[3]
        gw_codec_list = request_params[4]
        gw_timeout_list = request_params[5]
        gw_retry_list = request_params[6]

        if gw_list:
            if gw_codec_list:
                originate_str = "%s,absolute_codec_string=%s" \
                                        % (originate_str, gw_codec_list[0])
            if gw_timeout_list:
                originate_str = "%s,originate_timeout=%s" \
                                        % (originate_str, gw_timeout_list[0])

            outbound_str = "'socket:%s async full' inline" \
                                                % (self.fs_outbound_address)
            dial_str = "%s}%s/%s %s" \
                            % (originate_str, gw_list[0], to, outbound_str)
            bg_api_response = self.bgapi(dial_str)
            job_uuid = bg_api_response.get_job_uuid()
            self.bk_jobs[job_uuid] = request_uuid
            if not job_uuid:
                self.log.error("Calls Failed -- JobUUID not received"
                                                            % dial_str)
            # Reduce one from the call request param lists each time
            if gw_retry_list:
                gw_tries_done += 1
                if gw_tries_done > int(gw_retry_list[0]):
                    gw_tries_done = 0
                    request_params[3] = gw_list[1:]
                    request_params[4] = gw_codec_list[1:]
                    request_params[5] = gw_timeout_list[1:]
                    request_params[6] = gw_retry_list[1:]

            request_params[2] = gw_tries_done
            self.call_request[request_uuid] = request_params

    def bulk_originate(self, request_uuid_list):
        if request_uuid_list:
            job_pool = pool.Pool(len(request_uuid_list))
            [job_pool.spawn(self.spawn_originate, request_uuid)
                                        for request_uuid in request_uuid_list]

    def transfer_call(self, new_xml_url, call_uuid):
        self.set_var("plivo_transfer_url", new_xml_url, uuid=call_uuid)
        outbound_str = "socket:%s async full" \
                        % (self.fs_outbound_address)
        self.xfer_jobs[call_uuid] = outbound_str
        res = self.api("uuid_transfer %s 'sleep:5000' inline" % call_uuid)
        if res.is_success():
            self.log.info("Spawning Live Call Transfer for %s" % call_uuid)
            return True
        try:
            del self.xfer_jobs[call_uuid]
        except KeyError:
            pass
        self.log.error("Spawning Live Call Transfer Failed for %s : %s" \
                        % (call_uuid, str(res.get_response())))
        return False

    def hangup_call(self, call_uuid="", request_uuid=""):
        if not call_uuid and not request_uuid:
            self.log.error("Call Hangup Failed -- Missing call_uuid or request_uuid")
            return
        if call_uuid:
            callid = "CallUUID %s" % call_uuid
            args = "NORMAL_CLEARING uuid %s" % call_uuid
        else:  # Use request uuid
            callid = "RequestUUID %s" % request_uuid
            args = "NORMAL_CLEARING plivo_request_uuid %s" % request_uuid
        bg_api_response = self.bgapi("hupall %s" % args)
        job_uuid = bg_api_response.get_job_uuid()
        if not job_uuid:
            self.log.error("Call Hangup Failed for %s -- JobUUID not received" % callid)
            return False
        self.log.info("Executed Call hangup for %s" % callid)
        return True

    def hangup_all_calls(self):
        bg_api_response = self.bgapi("hupall NORMAL_CLEARING")
        job_uuid = bg_api_response.get_job_uuid()
        if not job_uuid:
            self.log.error("Hangup All Calls Failed -- JobUUID not received")
            return
        self.log.info("Executed Hangup for all calls")

