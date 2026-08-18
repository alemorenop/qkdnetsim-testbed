# --- CRYPTOPP Support ---
set(NS3_WITH_CRYPTOPP "" CACHE PATH "Build with CRYPTOPP support")
set(NS3_CRYPTOPP "OFF" CACHE INTERNAL "ON if CRYPTOPP is found in NS3_WITH_CRYPTOPP")

find_external_library(
  DEPENDENCY_NAME CRYPTOPP
  HEADER_NAME cryptlib.h
  LIBRARY_NAME crypto++ cryptopp
  SEARCH_PATHS /usr/include/cryptopp ${NS3_WITH_CRYPTOPP}
)

if(NOT CRYPTOPP_FOUND)
  message(STATUS "Skipping examples requiring CRYPTOPP")
else()
  include_directories(${CRYPTOPP_INCLUDE_DIRS})
  set(NS3_CRYPTOPP "ON" CACHE INTERNAL "ON if CRYPTOPP is found in NS3_WITH_CRYPTOPP")
endif()

# --- UUID Support ---
set(NS3_WITH_UUID "" CACHE PATH "Build with UUID support")
set(NS3_UUID "OFF" CACHE INTERNAL "ON if UUID is found in NS3_WITH_UUID")

find_external_library(
  DEPENDENCY_NAME uuid
  HEADER_NAME uuid.h
  LIBRARY_NAME uuid
  SEARCH_PATHS /usr/include/uuid ${NS3_WITH_UUID}
)

if(uuid_FOUND)
  include_directories(${uuid_INCLUDE_DIRS})
  link_libraries(${uuid_LIBRARIES})
  set(NS3_UUID "ON" CACHE INTERNAL "ON if UUID is found in NS3_WITH_UUID")
endif()

# --- Common Libraries for Examples ---
set(EXAMPLES_COMMON_LIBS
    ${libinternet}
    ${libqkdnetsim}
    ${libapplications}
    ${libmpi}
    ${libpoint-to-point}
    ${libnetwork}
    ${libcore}
    ${libmobility}
    ${libnetanim}
    ${CRYPTOPP_LIBRARIES}
)

# --- Macro to reduce repetition ---
macro(build_qkd_example name source)
    build_lib_example(
        NAME ${name}
        SOURCE_FILES ${source}
        LIBRARIES_TO_LINK ${EXAMPLES_COMMON_LIBS}
    )
endmacro()

# --- Build Examples ---
build_qkd_example(examples_qkdnetsim_etsi_014 examples_qkdnetsim_etsi_014.cc)
build_qkd_example(examples_qkdnetsim_etsi_004 examples_qkdnetsim_etsi_004.cc)
build_qkd_example(examples_qkdnetsim_etsi_combined_input examples_qkdnetsim_etsi_combined_input.cc)
build_qkd_example(examples_qkdnetsim_secoqc examples_qkdnetsim_secoqc.cc)
build_qkd_example(examples_qkdnetsim_etsi_014_emulation_tap examples_qkdnetsim_etsi_014_emulation_tap.cc)
build_qkd_example(examples_qkdnetsim_etsi_014_emulation examples_qkdnetsim_etsi_014_emulation.cc)

# --- Point-to-point QKD/KMS infrastructure used by the VPN scenarios ---
build_qkd_example(p2p_pp_alice point-to-point/pp_alice.cc)
build_qkd_example(p2p_pp_bob point-to-point/pp_bob.cc)
build_qkd_example(p2p_kms_alice point-to-point/kms_alice.cc)
build_qkd_example(p2p_kms_bob point-to-point/kms_bob.cc)

# --- Key-relay QKD/KMS infrastructure used by the VPN scenarios ---
build_qkd_example(relay_pp_alice key-relay/pp_alice.cc)
build_qkd_example(relay_pp_relay_a key-relay/pp_relay_a.cc)
build_qkd_example(relay_pp_relay_b key-relay/pp_relay_b.cc)
build_qkd_example(relay_pp_bob key-relay/pp_bob.cc)
build_qkd_example(relay_kms_alice key-relay/kms_alice.cc)
build_qkd_example(relay_kms_trusted key-relay/kms_trusted.cc)
build_qkd_example(relay_kms_bob key-relay/kms_bob.cc)
