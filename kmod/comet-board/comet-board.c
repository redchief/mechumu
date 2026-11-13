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
 */
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/string.h>
#include <linux/workqueue.h>

#define IMU_GIC_SPI 200	/* unused on QEMU's virt machine */

static struct device_node *imu_node;

struct pending {
	struct work_struct work;
	struct i2c_adapter *adap;
};

static bool is_sensor_bus(struct i2c_adapter *adap)
{
	/* The Virtual Comet's only i2c-tiny-usb adapter; the extension connector's is a DLN-2. */
	return !strncmp(adap->name, "i2c-tiny-usb", 12);
}

static void add_sensors(struct work_struct *work)
{
	struct pending *p = container_of(work, struct pending, work);
	struct i2c_board_info info[] = {
		{ I2C_BOARD_INFO("icm42605", 0x69), .fwnode = of_fwnode_handle(imu_node) },
		{ I2C_BOARD_INFO("opt3001", 0x44) },
		{ I2C_BOARD_INFO("lis2mdl", 0x1e) },
	};
	int i;

	for (i = 0; i < ARRAY_SIZE(info); i++) {
		struct i2c_client *c = i2c_new_client_device(p->adap, &info[i]);

		if (IS_ERR(c))
			dev_warn(&p->adap->dev, "comet-board: %s: %ld\n", info[i].type, PTR_ERR(c));
	}
	put_device(&p->adap->dev);
	kfree(p);
}

/* Not from inside the adapter's own registration: defer to a work item holding a reference. */
static void queue_sensors(struct i2c_adapter *adap)
{
	struct pending *p = kzalloc(sizeof(*p), GFP_KERNEL);

	if (!p)
		return;
	get_device(&adap->dev);
	p->adap = adap;
	INIT_WORK(&p->work, add_sensors);
	schedule_work(&p->work);
}

static int adapter_added(struct notifier_block *nb, unsigned long action, void *data)
{
	struct i2c_adapter *adap = i2c_verify_adapter(data);

	if (action == BUS_NOTIFY_ADD_DEVICE && adap && is_sensor_bus(adap))
		queue_sensors(adap);
	return NOTIFY_DONE;
}

static struct notifier_block notifier = { .notifier_call = adapter_added };

static int existing_adapter(struct device *dev, void *data)
{
	struct i2c_adapter *adap = i2c_verify_adapter(dev);

	if (adap && is_sensor_bus(adap))
		queue_sensors(adap);
	return 0;
}

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
	ret = bus_register_notifier(&i2c_bus_type, &notifier);
	if (ret)
		return ret;
	i2c_for_each_dev(NULL, existing_adapter);
	return 0;
}
module_init(comet_board_init);
/* No module_exit: the devices it registers keep using its devicetree node. */

MODULE_DESCRIPTION("Mecha Comet board sensors for the Virtual Comet (mechumu)");
MODULE_LICENSE("GPL");
