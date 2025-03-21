# Copyright 2021 Google Inc. All Rights Reserved.
# Modifications Copyright 2022-2023 Aerleon Project Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Openconfig yang ACL generator.

More information about the Openconfig ACL model schema:
http://ops.openconfig.net/branches/models/master/openconfig-acl.html
"""

import copy
import json
import sys
from collections import defaultdict
from typing import Any, DefaultDict, Dict, List, Set, Tuple, Union

from absl import logging

from aerleon.lib import aclgenerator, policy

if sys.version_info < (3, 8):
    from typing_extensions import TypedDict
else:
    from typing import TypedDict


class Error(aclgenerator.Error):
    """Generic error class."""


class OcFirewallError(Error):
    """Raised with problems in formatting for OpenConfig firewall."""


class ExceededAttributeCountError(Error):
    """Raised when the total attribute count of a policy is above the maximum."""


class TcpEstablishedWithNonTcpError(Error):
    """Raised when the TCP established option is set with a non TCP protocol."""


# Graceful handling of dict heierarchy for OpenConfig JSON.
def RecursiveDict() -> DefaultDict[Any, Any]:
    return defaultdict(RecursiveDict)


TransportConfig = TypedDict(
    "TransportConfig",
    {
        "source-port": Union[int, str],
        "destination-port": Union[int, str],
        "detail-mode": str,
        "builtin-detail": str,
    },
)
Transport = TypedDict("Transport", {"transport": TransportConfig})
IPConfig = TypedDict(
    "IPConfig", {"source-address": str, "destination-address": str, "protocol": int}
)
IP = TypedDict("IP", {"config": IPConfig})
ActionConfig = TypedDict("ActionConfig", {"forwarding-action": str})
Action = TypedDict("Action", {"config": ActionConfig})
ACLEntry = TypedDict(
    "ACLEntry",
    {"sequence-id": int, "actions": Action, "ipv4": IP, "ipv6": IP, "transport": Transport},
)
aclEntries = TypedDict("aclEntries", {"acl-entry": List[ACLEntry]})
ACLSetConfig = TypedDict("ACLSetConfig", {"name": str, "type": str})

ACLSet = TypedDict(
    "ACLSet", {"acl-entries": aclEntries, "config": ACLSetConfig, "name": str, "type": str}
)


class Term(aclgenerator.Term):
    """Creates the term for the OpenConfig firewall."""

    ACTION_MAP = {'accept': 'ACCEPT', 'deny': 'DROP', 'reject': 'REJECT'}

    # OpenConfig ip-protocols always will resolve to an 8-bit int, but these
    # common names are more convenient in a policy file.
    _ALLOW_PROTO_NAME = frozenset(['tcp', 'udp', 'icmp', 'esp', 'ah', 'ipip', 'sctp'])

    AF_RENAME = {
        4: 'ipv4',
        6: 'ipv6',
    }

    def __init__(self, term: policy.Term, inet_version: str = 'inet') -> None:
        super().__init__(term)
        self.term = term
        self.inet_version = inet_version

        # Combine (flatten) addresses with their exclusions into a resulting
        # flattened_saddr, flattened_daddr, flattened_addr.
        self.term.FlattenAll()

    def __str__(self) -> None:
        """Convert term to a string."""
        rules = self.ConvertToDict()
        json.dumps(rules, indent=2)

    def _tcp_established(self) -> Dict[str, str]:
        """Return's openconfig TCP_ESTABLISHED configuration.

        Other vendors (eg. SONiC) have slighly different implementations,
        This function permits inheritance."""
        return {'detail-mode': 'BUILTIN', 'builtin-detail': "TCP_ESTABLISHED"}

    def ConvertToDict(self, filter_options: List[str]) -> List[ACLEntry]:
        """Convert term to a dictionary.

        This is used to get a dictionary describing this term which can be
        output easily as an Openconfig JSON blob. It represents an "acl-entry"
        message from the OpenConfig ACL schema.

        Returns:
          A list of dictionaries that contains all fields necessary to create or
          update a OpenConfig acl-entry.
        """
        self.term_dict = RecursiveDict()

        # Rules will hold all exploded acl-entry dictionaries.
        rules = []

        # Convert the integer to the proper openconfig schema name str, ipv4/ipv6.
        term_af = self.AF_MAP.get(self.inet_version)
        family = self.AF_RENAME[term_af]

        self.SetName(self.term.name)

        # Action
        self.SetAction(filter_options)

        # Ballot fatigue handling for 'any'.
        saddrs = self.term.GetAddressOfVersion('flattened_saddr', term_af)
        if not saddrs:
            saddrs = ['any']

        daddrs = self.term.GetAddressOfVersion('flattened_daddr', term_af)
        if not daddrs:
            daddrs = ['any']

        sports = self.term.source_port
        if not sports:
            sports = [(0, 0)]

        dports = self.term.destination_port
        if not dports:
            dports = [(0, 0)]

        protos = self.term.protocol
        if not protos:
            protos = ['none']

        self.term_dict = copy.deepcopy(self.term_dict)

        if self.term.comment:
            self.SetComments(self.term.comment)

        # Options
        self.SetOptions(family, filter_options)

        # Source Addresses
        for saddr in saddrs:
            if saddr != 'any':
                self.SetSourceAddress(family, str(saddr), filter_options)

            # Destination Addresses
            for daddr in daddrs:
                if daddr != 'any':
                    self.SetDestAddress(family, str(daddr), filter_options)

                # Source Port
                for start, end in sports:
                    # 'any' starts and ends with zero.
                    if not start == end == 0:
                        self.SetSourcePorts(start, end, filter_options)

                    # Destination Port
                    for start, end in dports:
                        if not start == end == 0:
                            self.SetDestPorts(start, end, filter_options)

                        # Protocol
                        for proto in protos:
                            if isinstance(proto, str):
                                if proto != 'none':
                                    try:
                                        proto_num = self.PROTO_MAP[proto]
                                    except KeyError:
                                        raise OcFirewallError(
                                            'Protocol %s unknown. Use an integer.', proto
                                        )
                                    self.SetProtocol(family, proto_num, filter_options)
                            else:
                                self.SetProtocol(family, proto, filter_options)

                            # This is the business end of ace explosion.
                            # A dict is a reference type, so deepcopy is actually required.
                            rules.append(copy.deepcopy(self.term_dict))

        return rules

    def SetName(self, name: str) -> None:
        pass

    def SetAction(self, filter_options: List[str]) -> None:
        action = self.ACTION_MAP[self.term.action[0]]
        self.term_dict['actions'] = {}
        self.term_dict['actions']['config'] = {}
        self.term_dict['actions']['config']['forwarding-action'] = action

    def SetComments(self, comments: List[str]) -> None:
        pass

    def SetOptions(self, family: str, filter_options: List[str]) -> None:
        # options, 'family' unused
        if self.term.option:
            if 'tcp-established' in self.term.option:
                if self.term.protocol != ['tcp']:
                    raise TcpEstablishedWithNonTcpError(
                        f'tcp-established can only be used with tcp protocol in term {self.term.name}'
                    )
                self.term_dict['transport']['config'].update(self._tcp_established())

    def SetSourceAddress(self, family: str, saddr: str, filter_options: List[str]) -> None:
        self.term_dict[family]['config']['source-address'] = saddr

    def SetDestAddress(self, family: str, daddr: str, filter_options: List[str]) -> None:
        self.term_dict[family]['config']['destination-address'] = daddr

    def SetSourcePorts(self, start: int, end: int, filter_options: List[str]) -> None:
        if start == end:
            self.term_dict['transport']['config']['source-port'] = start
        else:
            self.term_dict['transport']['config']['source-port'] = '%d..%d' % (
                start,
                end,
            )

    def SetDestPorts(self, start: int, end: int, filter_options: List[str]) -> None:
        if start == end:
            self.term_dict['transport']['config']['destination-port'] = start
        else:
            self.term_dict['transport']['config']['destination-port'] = '%d..%d' % (
                start,
                end,
            )

    def SetProtocol(self, family: str, protocol: int, filter_options: List[str]) -> None:
        self.term_dict[family]['config']['protocol'] = protocol


class OpenConfig(aclgenerator.ACLGenerator):
    """A OpenConfig firewall policy object."""

    _PLATFORM = 'openconfig'
    SUFFIX = '.oacl'
    _SUPPORTED_AF = frozenset(('inet', 'inet6', 'mixed'))
    FAMILY_MAP = {'mixed': 'ACL_MIXED', 'inet6': 'ACL_IPV6', 'inet': 'ACL_IPV4'}
    _TERM = Term

    def _BuildTokens(self) -> Tuple[Set[str], Dict[str, Set[str]]]:
        """Build supported tokens for platform.

        Returns:
          tuple containing both supported tokens and sub tokens
        """
        supported_tokens, supported_sub_tokens = super()._BuildTokens()

        # Remove unsupported things
        supported_tokens -= {'platform', 'platform_exclude', 'icmp-type', 'verbatim'}

        # OpenConfig ACL model only supports these three forwarding actions.
        supported_sub_tokens['action'] = {'accept', 'deny', 'reject'}

        supported_sub_tokens.update(
            {
                'option': {
                    'tcp-established',
                }
            }
        )

        return supported_tokens, supported_sub_tokens

    def _InitACLSet(self) -> None:
        """Initialize self.acl_sets with proper Typing"""
        self.acl_sets: List[ACLSet] = []

    def _TranslatePolicy(self, pol: policy.Policy, exp_info: int) -> None:
        self.total_rule_count = 0
        self._InitACLSet()

        for header, terms in pol.filters:
            filter_options = header.FilterOptions(self._PLATFORM)
            filter_name = header.FilterName(self._PLATFORM)

            # Options are anything after the platform name in the target message of
            # the policy header, [1:].

            # Get the address family if set.
            address_family = 'inet'
            for i in self._SUPPORTED_AF:
                if i in filter_options:
                    address_family = i
                    filter_options.remove(i)
            self._TranslateTerms(
                terms, address_family, filter_name, header.comment, filter_options
            )

        logging.info('Total rule count of policy %s is: %d', filter_name, self.total_rule_count)

    def _TranslateTerms(
        self,
        terms: List[Term],
        address_family: str,
        filter_name: str,
        hdr_comments: List[str],
        filter_options: List[str],
    ) -> None:
        """
        Factor out the translation of terms, such that it can be overridden by subclasses
        """
        oc_acl_entries: List[ACLEntry] = []

        for term in terms:
            # Handle mixed for each indvidual term as inet and inet6.
            # inet/inet6 are treated the same.
            term_address_families = []
            if address_family == 'mixed':
                term_address_families = ['inet', 'inet6']
            else:
                term_address_families = [address_family]
            for term_af in term_address_families:
                t = self._TERM(term, term_af)
                for rule in t.ConvertToDict(filter_options):
                    self.total_rule_count += 1
                    rule['sequence-id'] = self.total_rule_count * 5
                    oc_acl_entries.append(rule)
        oc_type = self.FAMILY_MAP[address_family]
        oc_acl_set = {
            "acl-entries": {"acl-entry": oc_acl_entries},
            "config": {"name": filter_name, "type": oc_type},
            "name": filter_name,
            "type": oc_type,
        }
        self.acl_sets.append(oc_acl_set)

    def __str__(self) -> str:
        out = '%s\n\n' % (
            json.dumps(self.acl_sets, indent=2, separators=(',', ': '), sort_keys=True)
        )

        return out
