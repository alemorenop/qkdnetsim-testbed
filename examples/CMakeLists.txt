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

# --- Point-to-point scenario (Fig. 3 del paper, un proceso independiente por host/contenedor) ---
build_qkd_example(p2p_host1_pp_alice point-to-point/host1_pp_alice.cc)
build_qkd_example(p2p_host2_pp_bob point-to-point/host2_pp_bob.cc)
build_qkd_example(p2p_host3_kms_alice point-to-point/host3_kms_alice.cc)
build_qkd_example(p2p_host4_kms_bob point-to-point/host4_kms_bob.cc)
build_qkd_example(p2p_host5_etsi014_alice point-to-point/host5_etsi014_alice.cc)
build_qkd_example(p2p_host6_etsi014_bob point-to-point/host6_etsi014_bob.cc)

# --- Key-relay scenario (Alice-Relay-Bob, sin enlace directo Alice-Bob) ---
build_qkd_example(relay_host1_pp_alice key-relay/host1_pp_alice.cc)
build_qkd_example(relay_host2_pp_relay_a key-relay/host2_pp_relay_a.cc)
build_qkd_example(relay_host3_pp_relay_b key-relay/host3_pp_relay_b.cc)
build_qkd_example(relay_host4_pp_bob key-relay/host4_pp_bob.cc)
build_qkd_example(relay_host5_kms_alice key-relay/host5_kms_alice.cc)
build_qkd_example(relay_host6_kms_relay key-relay/host6_kms_relay.cc)
build_qkd_example(relay_host7_kms_bob key-relay/host7_kms_bob.cc)
build_qkd_example(relay_host8_etsi014_alice key-relay/host8_etsi014_alice.cc)
build_qkd_example(relay_host9_etsi014_bob key-relay/host9_etsi014_bob.cc)
