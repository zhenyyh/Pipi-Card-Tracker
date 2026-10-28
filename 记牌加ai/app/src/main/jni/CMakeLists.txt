cmake_minimum_required(VERSION 3.18)
project(pctdobby C CXX)

# Dobby 需要自己编：https://github.com/jmpews/Dobby
# 把编出来的 libdobby.a 放到 jni/dobby/<abi>/ 下（本仓库不带二进制）
set(DOBBY_DIR ${CMAKE_SOURCE_DIR}/dobby/${ANDROID_ABI})
add_library(dobby STATIC IMPORTED)
set_target_properties(dobby PROPERTIES IMPORTED_LOCATION ${DOBBY_DIR}/libdobby.a)

add_library(pctdobby SHARED dobby_hook.cpp)
target_link_libraries(pctdobby dobby log)
