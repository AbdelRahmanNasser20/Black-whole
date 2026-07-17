#!/bin/sh
# Deploy the comps service + canonical parser to the Pi and restart it.
set -e
scp scripts/pi_comps_service.py black-whole:~/comps/comps_service.py
scp deals/ebay_parse.py black-whole:~/comps/ebay_parse.py
ssh black-whole 'systemctl --user restart comps.service && sleep 2 && systemctl --user is-active comps.service'
