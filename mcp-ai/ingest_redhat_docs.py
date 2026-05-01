#!/usr/bin/env python3
"""Ingest curated Red Hat product documentation into HAL training data.

This script targets a bounded set of Red Hat documentation roots and crawls only
the relevant product/version pages so the resulting training corpus is useful
for HAL without pulling in unrelated site footer/navigation content.

Supported doc sets:
  - satellite-6.18
  - aap-2.6
  - idm-5.0   (mapped to the RHEL 10 Identity Management documentation set)
"""

from __future__ import annotations

import argparse
import sys

from ingest_urls import crawl


DOCSETS = {
    'satellite-6.18': {
        'label': 'Red Hat Satellite 6.18',
        'start_urls': [
            'https://docs.redhat.com/en/documentation/red_hat_satellite/6.18',
        ],
        'allow_prefixes': [
            'https://docs.redhat.com/en/documentation/red_hat_satellite/6.18',
        ],
    },
    'aap-2.6': {
        'label': 'Red Hat Ansible Automation Platform 2.6',
        'start_urls': [
            'https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6',
        ],
        'allow_prefixes': [
            'https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6',
        ],
    },
    'idm-5.0': {
        'label': 'Identity Management docs (mapped to RHEL 10 IdM documentation set)',
        'start_urls': [
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/installing_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/planning_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_ansible_to_install_and_manage_identity_management_in_rhel',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/installing_trust_between_idm_and_ad',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_certificates_in_idm',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/accessing_identity_management_services',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/migrating_to_identity_management_on_rhel_10',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_idm_users_groups_hosts_and_access_control_rules',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_replication_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/tuning_performance_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/preparing_for_disaster_recovery_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/performing_disaster_recovery_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/working_with_dns_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_external_red_hat_utilities_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_idm_api',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_idm_healthcheck_to_monitor_your_idm_environment',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_smart_card_authentication',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/working_with_vaults_in_identity_management',
        ],
        'allow_prefixes': [
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/installing_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/planning_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_ansible_to_install_and_manage_identity_management_in_rhel',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/installing_trust_between_idm_and_ad',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_certificates_in_idm',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/accessing_identity_management_services',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/migrating_to_identity_management_on_rhel_10',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_idm_users_groups_hosts_and_access_control_rules',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_replication_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/tuning_performance_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/preparing_for_disaster_recovery_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/performing_disaster_recovery_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/working_with_dns_in_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_external_red_hat_utilities_with_identity_management',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_idm_api',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/using_idm_healthcheck_to_monitor_your_idm_environment',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/managing_smart_card_authentication',
            'https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/working_with_vaults_in_identity_management',
        ],
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Ingest curated Red Hat product documentation into HAL training data')
    parser.add_argument('docsets', nargs='*', help='Doc set keys to ingest (default: all supported doc sets)')
    parser.add_argument('--depth', type=int, default=1, help='Crawl depth for each seed URL (default: 1)')
    parser.add_argument('--max-pages', type=int, default=400, help='Maximum total pages to fetch across selected doc sets')
    parser.add_argument('--timeout', type=int, default=15, help='Per-request timeout seconds')
    parser.add_argument('--list-docsets', action='store_true', help='List supported doc set keys and exit')
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_docsets:
        for key, info in DOCSETS.items():
            print(f'{key:15} {info["label"]}')
        return 0

    selected = args.docsets or list(DOCSETS.keys())

    # Resolve URL arguments to their matching docset keys so that both
    # `satellite-6.18` and `https://docs.redhat.com/.../satellite/6.18` work.
    def _url_to_docset(item: str) -> str:
        if item in DOCSETS:
            return item
        if item.startswith('http://') or item.startswith('https://'):
            for key, info in DOCSETS.items():
                if any(item.rstrip('/') == u.rstrip('/') or item.rstrip('/').startswith(u.rstrip('/'))
                       for u in info['start_urls'] + info['allow_prefixes']):
                    return key
        return item

    selected = [_url_to_docset(s) for s in selected]

    unknown = [item for item in selected if item not in DOCSETS]
    if unknown:
        print('Unknown doc set(s): ' + ', '.join(unknown), file=sys.stderr)
        print('Supported doc sets: ' + ', '.join(sorted(DOCSETS.keys())), file=sys.stderr)
        return 2

    start_urls = []
    allow_prefixes = []
    for key in selected:
        info = DOCSETS[key]
        start_urls.extend(info['start_urls'])
        allow_prefixes.extend(info['allow_prefixes'])

    print('Ingesting Red Hat documentation into HAL training data:')
    for key in selected:
        print(f'  - {key}: {DOCSETS[key]["label"]}')
    print(f'  Seeds      : {len(start_urls)}')
    print(f'  Depth      : {args.depth}')
    print(f'  Max pages  : {args.max_pages}')
    print(f'  Timeout    : {args.timeout}s')
    print('')

    saved = crawl(
        start_urls,
        max_depth=args.depth,
        max_pages=args.max_pages,
        timeout=args.timeout,
        allow_prefixes=allow_prefixes,
        same_host_only=True,
    )

    print('')
    print(f'Completed. Saved {len(saved)} document page(s) into ~/.mcp-ai/training.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())