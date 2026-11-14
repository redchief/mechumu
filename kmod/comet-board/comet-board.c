// SPDX-License-Identifier: GPL-2.0
/*
 * comet-board: the Mecha Comet's I2C3 sensors, for the Virtual Comet (mechumu).
 *
 * On a Comet the devicetree (imx8mp-mecha-comet.dts) declares an ICM-42605 IMU at 0x69, an
 * OPT3001 light sensor at 0x44 and an LIS2MDL magnetometer at 0x1e on I2C3. QEMU's virt machine has
 * no such bus, so mechumu's host side serves one as an i2c-tiny-usb adapter over USB/IP, and this
 * module registers the same devices on it when it appears; their mainline drivers do the rest.
 *
 * The IMU's driver takes its chip type and interrupt from the device's firmware node, as on the
 * Comet, so the IMU gets a devicetree node like the Comet's (the Virtual Comet boots with a
 * devicetree, as a Comet does). Its interrupt is a GIC line no device drives: raw reads and
 * iio-sensor-proxy's polling work, FIFO-buffered capture doesn't. The light sensor and
 * magnetometer get no interrupt, so their drivers poll.
 *
 * It also puts a device on the extension connector's SPI pins (the host's DLN-2 adapter): Linux
 * only creates SPI devices from firmware, and the Comet's would come from an extension's overlay.
 * It's named comet-ext-spi, and a udev rule binds spidev to it (/dev/spidev-comet-ext).
 *
 * And it drives the connector's PWM3 and PWM4 pins: the DLN-2 adapter has a second, vendor
 * interface for them (mainline dln2 takes only interface 0), and this module's USB driver gives
 * each its own one-channel pwmchip, as the Comet's i.MX PWM blocks are. Each setting goes to the
 * host as one control request.
 */
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pwm.h>
#include <linux/spi/spi.h>
#include <linux/string.h>
#include <linux/usb.h>
#include <linux/workqueue.h>

#define IMU_GIC_SPI 200	/* unused on QEMU's virt machine */

static struct device_node *imu_node;

struct pending {
	struct work_struct work;
	struct device *dev;
};

static bool is_sensor_bus(struct i2c_adapter *adap)
{
	/* The Virtual Comet's only i2c-tiny-usb adapter; the extension connector's is a DLN-2. */
	return !strncmp(adap->name, "i2c-tiny-usb", 12);
}

static void add_sensors(struct work_struct *work)
{
	struct pending *p = container_of(work, struct pending, work);
	struct i2c_adapter *adap = to_i2c_adapter(p->dev);
	struct i2c_board_info info[] = {
		{ I2C_BOARD_INFO("icm42605", 0x69), .fwnode = of_fwnode_handle(imu_node) },
		{ I2C_BOARD_INFO("opt3001", 0x44) },
		{ I2C_BOARD_INFO("lis2mdl", 0x1e) },
	};
	int i;

	for (i = 0; i < ARRAY_SIZE(info); i++) {
		struct i2c_client *c = i2c_new_client_device(adap, &info[i]);

		if (IS_ERR(c))
			dev_warn(&adap->dev, "comet-board: %s: %ld\n", info[i].type, PTR_ERR(c));
	}
	put_device(p->dev);
	kfree(p);
}

static bool is_connector_spi(struct device *dev)
{
	return dev_is_platform(dev) && !strncmp(dev_name(dev), "dln2-spi", 8);
}

static void add_spidev(struct work_struct *work)
{
	struct pending *p = container_of(work, struct pending, work);
	struct spi_board_info info = { .modalias = "comet-ext-spi" };
	struct spi_device *spi;

	device_lock(p->dev);	/* the bridge can be unplugged meanwhile; its controller goes with it */
	if (p->dev->driver && dev_get_drvdata(p->dev)) {
		spi = spi_new_device(dev_get_drvdata(p->dev), &info);
		if (!spi)
			dev_warn(p->dev, "comet-board: no SPI device\n");
	}
	device_unlock(p->dev);
	put_device(p->dev);
	kfree(p);
}

/* Not from inside the device's own registration or probe: defer to a work item holding a reference. */
static void queue(struct device *dev, work_func_t fn)
{
	struct pending *p = kzalloc(sizeof(*p), GFP_KERNEL);

	if (!p)
		return;
	p->dev = get_device(dev);
	INIT_WORK(&p->work, fn);
	schedule_work(&p->work);
}

static int adapter_added(struct notifier_block *nb, unsigned long action, void *data)
{
	struct i2c_adapter *adap = i2c_verify_adapter(data);

	if (action == BUS_NOTIFY_ADD_DEVICE && adap && is_sensor_bus(adap))
		queue(&adap->dev, add_sensors);
	return NOTIFY_DONE;
}

static struct notifier_block i2c_notifier = { .notifier_call = adapter_added };

static int existing_adapter(struct device *dev, void *data)
{
	struct i2c_adapter *adap = i2c_verify_adapter(dev);

	if (adap && is_sensor_bus(adap))
		queue(dev, add_sensors);
	return 0;
}

/* spi-dln2 has registered its controller (the platform device's drvdata) once it's bound. */
static int spi_bound(struct notifier_block *nb, unsigned long action, void *data)
{
	if (action == BUS_NOTIFY_BOUND_DRIVER && is_connector_spi(data))
		queue(data, add_spidev);
	return NOTIFY_DONE;
}

static struct notifier_block platform_notifier = { .notifier_call = spi_bound };

static int existing_spi(struct device *dev, void *data)
{
	if (is_connector_spi(dev) && dev->driver)
		queue(dev, add_spidev);
	return 0;
}

/* --- connector PWM3, PWM4 ------------------------------------------------------------------- */
#define PWM_PINS	2
#define REQ_SET_PWM	0x01	/* vendor request to the interface; wValue: 0 PWM3, 1 PWM4 */

struct comet_pwm {
	struct usb_interface *intf;
	u16 pin;
};

static int comet_pwm_apply(struct pwm_chip *chip, struct pwm_device *pwm, const struct pwm_state *state)
{
	struct comet_pwm *p = pwmchip_get_drvdata(chip);
	struct {
		__le64 period_ns, duty_ns;
		u8 inversed, enabled;
	} __packed msg = {
		.period_ns = cpu_to_le64(state->period),
		.duty_ns = cpu_to_le64(state->duty_cycle),
		.inversed = state->polarity == PWM_POLARITY_INVERSED,
		.enabled = state->enabled,
	};

	return usb_control_msg_send(interface_to_usbdev(p->intf), 0, REQ_SET_PWM,
				    USB_DIR_OUT | USB_TYPE_VENDOR | USB_RECIP_INTERFACE, p->pin,
				    p->intf->cur_altsetting->desc.bInterfaceNumber, &msg, sizeof(msg),
				    USB_CTRL_SET_TIMEOUT, GFP_KERNEL);
}

static const struct pwm_ops comet_pwm_ops = { .apply = comet_pwm_apply };

static int comet_pwm_probe(struct usb_interface *intf, const struct usb_device_id *id)
{
	int pin, ret;

	for (pin = 0; pin < PWM_PINS; pin++) {
		struct pwm_chip *chip = devm_pwmchip_alloc(&intf->dev, 1, sizeof(struct comet_pwm));
		struct comet_pwm *p;

		if (IS_ERR(chip))
			return PTR_ERR(chip);
		p = pwmchip_get_drvdata(chip);
		p->intf = intf;
		p->pin = pin;
		chip->ops = &comet_pwm_ops;
		ret = devm_pwmchip_add(&intf->dev, chip);
		if (ret)
			return ret;
	}
	return 0;
}

static void comet_pwm_disconnect(struct usb_interface *intf)
{
}

static const struct usb_device_id comet_pwm_ids[] = {
	{ USB_DEVICE_INTERFACE_NUMBER(0xa257, 0x2013, 1) },	/* the connector bridge's PWM interface */
	{ }
};
MODULE_DEVICE_TABLE(usb, comet_pwm_ids);

static struct usb_driver comet_pwm_driver = {
	.name = "comet-pwm",
	.id_table = comet_pwm_ids,
	.probe = comet_pwm_probe,
	.disconnect = comet_pwm_disconnect,
};

/* /comet-i2c3/imu@69, as in the Comet's devicetree; the container is disabled so nothing else binds. */
static int add_imu_node(void)
{
	static struct of_changeset cs;
	const u32 interrupts[] = { 0 /* SPI */, IMU_GIC_SPI, 1 /* IRQ_TYPE_EDGE_RISING */ };
	struct device_node *bus;
	int ret;

	if (!of_root)
		return -ENODEV;	/* booted with ACPI: run QEMU with -machine virt,acpi=off */
	of_changeset_init(&cs);
	bus = of_changeset_create_node(&cs, of_root, "comet-i2c3");
	imu_node = bus ? of_changeset_create_node(&cs, bus, "imu@69") : NULL;
	if (!imu_node)
		return -ENOMEM;
	ret = of_changeset_add_prop_string(&cs, bus, "status", "disabled") ?:
	      of_changeset_add_prop_string(&cs, imu_node, "compatible", "invensense,icm42605") ?:
	      of_changeset_add_prop_u32_array(&cs, imu_node, "interrupts", interrupts, ARRAY_SIZE(interrupts)) ?:
	      of_changeset_apply(&cs);
	if (ret)
		of_changeset_destroy(&cs);
	return ret;
}

static int __init comet_board_init(void)
{
	int ret = add_imu_node();

	if (ret) {
		pr_warn("comet-board: no devicetree node for the IMU (%d); it won't probe\n", ret);
		imu_node = NULL;
	}
	ret = bus_register_notifier(&i2c_bus_type, &i2c_notifier);
	if (ret)
		return ret;
	ret = bus_register_notifier(&platform_bus_type, &platform_notifier);
	if (ret) {
		bus_unregister_notifier(&i2c_bus_type, &i2c_notifier);
		return ret;
	}
	ret = usb_register(&comet_pwm_driver);
	if (ret) {
		bus_unregister_notifier(&platform_bus_type, &platform_notifier);
		bus_unregister_notifier(&i2c_bus_type, &i2c_notifier);
		return ret;
	}
	i2c_for_each_dev(NULL, existing_adapter);
	bus_for_each_dev(&platform_bus_type, NULL, NULL, existing_spi);
	return 0;
}
module_init(comet_board_init);
/* No module_exit: the devices it registers keep using its devicetree node. */

MODULE_DESCRIPTION("Mecha Comet board sensors and connector SPI and PWM for the Virtual Comet (mechumu)");
MODULE_LICENSE("GPL");
