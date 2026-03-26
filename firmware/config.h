#ifndef __CONFIG_H_
#define __CONFIG_H_

#include <stdint.h>

typedef struct {
  char name[8];
  uint8_t sm_id;
  uint8_t clk_pin;
  uint8_t io_pin;
  uint32_t freq_hz;
} swd_cfg;

typedef enum {
  SNIFFER_TYPE_NONE = 0U,
  SNIFFER_TYPE_SPI,
  SNIFFER_TYPE_I2C,
  SNIFFER_TYPE_UART,
} sniffer_type;

typedef struct {
  uint8_t clk_pin;
  uint8_t mosi_pin;
  uint8_t miso_pin;
  uint8_t cs_pin;
  uint8_t txn_width;
} sniffer_spi_cfg;

typedef struct {
  uint8_t scl_pin;
  uint8_t sda_pin;
  uint8_t addr_filter;
} sniffer_i2c_cfg;

typedef struct {
  uint8_t tx_pin;
  uint8_t rx_pin;
  uint32_t baud_rate;
} sniffer_uart_cfg;

typedef struct {
  char name[8];
  sniffer_type type;
  uint8_t sm_id;
  union {
    sniffer_spi_cfg spi;
    sniffer_i2c_cfg i2c;
    sniffer_uart_cfg uart;
  } hw;
} sniffer_cfg;

typedef struct {
  char name[32];
  swd_cfg swds[4];
  uint8_t num_swd;
  sniffer_cfg sniffers[8];
  uint8_t num_sniffer;
} target_cfg;

extern const target_cfg stratum_cfg;

#endif // __CONFIG_H_
