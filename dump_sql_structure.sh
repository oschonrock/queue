#!/usr/bin/env bash

mysqldump -uqueue -pqwLK56vb --compact --no-data queue | \
    sed 's/ AUTO_INCREMENT=[0-9]*//g' | \
    egrep -v '/\*' | \
    sed '/^CREATE TABLE/ i\
' | \
    sed 's/`//g' > db/structure.sql
