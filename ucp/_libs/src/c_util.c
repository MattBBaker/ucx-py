/**
 * Copyright (c) 2018, NVIDIA CORPORATION. All rights reserved.
 * See file LICENSE for terms.
 */

#include "c_util.h"
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>



int c_util_get_ucp_listener_params(ucp_listener_params_t *param,
                                   uint16_t port,
                                   ucp_listener_accept_callback_t callback_func,
                                   void *callback_args) {

    /* The listener will listen on INADDR_ANY */
    struct sockaddr_in *listen_addr = malloc(sizeof(struct sockaddr_in));
    if(listen_addr == NULL) {
        return 1;
    }
    memset(listen_addr, 0, sizeof(struct sockaddr_in));
    listen_addr->sin_family      = AF_INET;
    listen_addr->sin_addr.s_addr = INADDR_ANY;
    listen_addr->sin_port        = htons(port);

	param->field_mask         = UCP_LISTENER_PARAM_FIELD_SOCK_ADDR |
                                UCP_LISTENER_PARAM_FIELD_ACCEPT_HANDLER;
	param->sockaddr.addr      = (const struct sockaddr *) listen_addr;
	param->sockaddr.addrlen   = sizeof(struct sockaddr_in);
	param->accept_handler.cb  = callback_func;
	param->accept_handler.arg = callback_args;
    return 0;
}


void c_util_get_ucp_listener_params_free(ucp_listener_params_t *param) {
    free((void*) param->sockaddr.addr);
}


int c_util_get_ucp_ep_params_ip(ucp_ep_params_t *param,
                             const char *ip_address,
                             uint16_t port) {

    struct sockaddr_in *connect_addr = malloc(sizeof(struct sockaddr_in));
    if(connect_addr == NULL) {
        return 1;
    }
    memset(connect_addr, 0, sizeof(struct sockaddr_in));
    connect_addr->sin_family      = AF_INET;
    connect_addr->sin_addr.s_addr = inet_addr(ip_address);
    connect_addr->sin_port        = htons(port);

	param->field_mask         = UCP_EP_PARAM_FIELD_FLAGS |
                                UCP_EP_PARAM_FIELD_SOCK_ADDR |
                                UCP_EP_PARAM_FIELD_ERR_HANDLING_MODE |
                                UCP_EP_PARAM_FIELD_ERR_HANDLER;
	param->err_mode           = UCP_ERR_HANDLING_MODE_NONE;
    param->flags              = UCP_EP_PARAMS_FLAGS_CLIENT_SERVER;
    param->err_handler.cb     = NULL;
    param->err_handler.arg    = NULL;
    param->sockaddr.addr      = (const struct sockaddr *) connect_addr;
	param->sockaddr.addrlen   = sizeof(struct sockaddr_in);
    return 0;
}

int c_util_get_ucp_ep_params_ucp(ucp_ep_params_t *param, ucp_address_t *addr) {
    param->flags           = UCP_EP_PARAM_FIELD_REMOTE_ADDRESS;
    param->address         = addr;
    printf("passing addr pointer: %x\n", addr);
    return 0;
}

void c_util_get_ucp_ep_params_free(ucp_ep_params_t *param) {
    if( param->field_mask & UCP_EP_PARAM_FIELD_SOCK_ADDR) {
        free((void*) param->sockaddr.addr);
    }
}
