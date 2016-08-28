#!/bin/sh

echo "- Getting latest Sjeng ..."

if [ -d Sjeng ]; then
    cd Sjeng
    make clean > /dev/null
    git pull
else
    git clone https://github.com/niklasf/Sjeng.git
    cd Sjeng
fi

echo "- Building"

make EXE=../sjeng-x86_64 CC=gcc

echo "- Done!"
