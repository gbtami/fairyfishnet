#!/bin/sh

echo "- Getting latest Sunsetter ..."

if [ -d Sunsetter ]; then
    cd Sunsetter
    make clean > /dev/null
    git pull
else
    git clone --depth 1 https://github.com/niklasf/Sunsetter.git
    cd Sunsetter
fi

echo "- Building"

make EXE=../sunsetter-x86_64 CC=gcc CXX=g++

echo "- Done!"
