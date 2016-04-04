#!/bin/sh

while [ true ]; do
    python -m fishnet "$@"
    ret=$?
    if [ $ret -eq 70 ]; then
        for try in 1 2 3; do
            pip download fishnet
            pip install --upgrade fishnet
            success=$?
            if [ $success -eq 0 ]; then
                break
            else
                sleep 10
            fi
        done

        if [ $success -ne 0 ]; then
          exit 70
        fi
    else
        exit $ret
    fi
done
