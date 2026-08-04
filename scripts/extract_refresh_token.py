#!/usr/bin/env python3
"""Extract a refresh token from YAML/ENV content or a file.

Usage:
  cat env.yml | ./scripts/extract_refresh_token.py
  ./scripts/extract_refresh_token.py /path/to/env.yml
"""
import sys
import re
try:
    import yaml
except Exception:
    yaml = None

def parse_text(s: str) -> str:
    # Try to parse YAML if possible
    if yaml:
        try:
            d = yaml.safe_load(s or "{}")
            if isinstance(d, dict):
                rh = d.get('redhat') or {}
                if isinstance(rh, dict) and rh.get('refresh_token'):
                    return rh.get('refresh_token')
                for k in ('RH_INSIGHTS_REFRESH_TOKEN','RH_INSIGHTS_REFRESH','REFRESH_TOKEN','refresh_token'):
                    if k in d and d.get(k):
                        return d.get(k)
        except Exception:
            pass
    # Fallback to regex search
    m = re.search(r'(?i)refresh_token\s*[:=]\s*[\'\"]?([A-Za-z0-9_\-\.\+/=]+)[\'\"]?', s)
    if m:
        return m.group(1)
    return ''

def main():
    if len(sys.argv) > 1:
        try:
            txt = open(sys.argv[1], 'r', encoding='utf-8').read()
        except Exception:
            txt = ''
    else:
        txt = sys.stdin.read()
    tok = parse_text(txt)
    if tok:
        print(tok)

if __name__ == '__main__':
    main()
