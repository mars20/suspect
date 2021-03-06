from suspect import MRSData

import struct
import numpy
import re

# This file largely relies on information from Siemens regarding the structure
# of the TWIX file formats. Most of the parameters that are read use the same
# names as those apparently used internally at Siemens. The purpose of some of
# these parameters is not currently clear but we break them out anyway.


class TwixBuilder(object):
    def __init__(self):
        self.header_params = None
        self.dt = None
        self.np = None
        self.num_channels = None
        self.data = []
        self.loop_counters = []

    def set_header_string(self, header_string):
        self.header_params = parse_twix_header(header_string)

    def set_np(self, np):
        if self.np is None:
            self.np = np
        else:
            if self.np != np:
                raise ValueError("TwixBuilder already has an np of {}, now trying to set a different value of {}".format(self.np, np))

    def set_num_channels(self, num_channels):
        if self.num_channels is None:
            self.num_channels = num_channels
        else:
            if self.num_channels != num_channels:
                raise ValueError("TwixBuilder num_channels already set to {}, now being changed to {}".format(self.num_channels, num_channels))

    def add_scan(self, loop_counters, scan_data):
        self.loop_counters.append(loop_counters)
        self.data.append(scan_data)

    def build_mrsdata(self):
        loop_counter_array = numpy.array(self.loop_counters)
        data_shape = 1 + numpy.max(loop_counter_array, axis=0)
        data_shape = numpy.append(data_shape, (self.num_channels, self.np))
        data = numpy.zeros(data_shape, dtype='complex')
        for i, loop_counter in enumerate(loop_counter_array):
            # have to break out the loop_counter parameters individually, the second line should work but doesn't
            data[loop_counter[0], loop_counter[1], loop_counter[2], loop_counter[3], loop_counter[4], loop_counter[5], loop_counter[6], loop_counter[7], loop_counter[8], loop_counter[9], loop_counter[10], loop_counter[11], loop_counter[12], loop_counter[13]] = self.data[i]
            #data[loop_counter] = self.data[i]

        # get rid of all the size 1 dimensions
        data = data.squeeze()

        metadata = {
            "patient_name": self.header_params["patient_name"],
            "patient_id": self.header_params["patient_id"],
            "patient_birthdate": self.header_params["patient_birthdate"]
        }
        mrs_data = MRSData(data, self.header_params["dt"], self.header_params["f0"], metadata=metadata)

        return mrs_data


def parse_twix_header(header_string):
    # get the name of the protocol being acquired
    protocol_name_string = re.search(r"<ParamString.\"tProtocolName\">  { \".+\"  }\n", header_string).group()
    protocol_name = protocol_name_string.split("\"")[3]
    # get information about the subject being scanned
    patient_id_string = re.search(r"<ParamString.\"PatientID\">  { \".+\"  }\n", header_string).group()
    patient_id = patient_id_string.split("\"")[3]
    patient_name = re.escape(re.search(r"(<ParamString.\"PatientName\">  { \")(.+)(\"  }\n)", header_string).group(2))
    patient_birthday = re.search(r"(<ParamString.\"PatientBirthDay\">  { \")(.+)(\"  }\n)", header_string).group(2)
    # get the scan parameters
    frequency_string = re.search(r"<ParamLong.\"Frequency\">  { \d*  }", header_string).group()
    number_string = re.search(r"[0-9]+", frequency_string).group()
    frequency = int(number_string) * 1e-6
    frequency_string = re.search(r"<ParamLong.\"DwellTimeSig\">  { \d*  }", header_string).group()
    number_string = re.search(r"[0-9]+", frequency_string).group()
    dwell_time = int(number_string) * 1e-9
    return {"protocol_name": protocol_name,
            "patient_name": patient_name,
            "patient_id": patient_id,
            "patient_birthdate": patient_birthday,
            "dt": dwell_time,
            "f0": frequency
            }


def load_twix_vb(fin, builder):

    # first four bytes are the size of the header
    header_size = struct.unpack("I", fin.read(4))[0]

    # read the rest of the header minus the four bytes we already read
    header = fin.read(header_size - 4)
    # for some reason the last 24 bytes of the header contain some junk that is not a string
    header = header[:-24].decode('latin-1')
    builder.set_header_string(header)

    # the way that vb files are set up we just keep reading scans until the acq_end flag is set

    while True:
        # start by keeping track of where in the file this scan started
        # this will be used to jump to the start of the next scan
        start_position = fin.tell()

        # the first four bytes contain composite information
        temp = struct.unpack("I", fin.read(4))[0]

        # 25 LSBs contain DMA length (size of this scan)
        DMA_length = temp & (2 ** 26 - 1)
        # next we have the "pack" flag bit and the rest is PCI_rx
        # not sure what either of these are for but break them out in case
        pack_flag = (temp >> 25) & 1
        PCI_rx = temp >> 26

        meas_uid, scan_counter, time_stamp, pmu_time_stamp = struct.unpack("IIII", fin.read(16))

        # next long int is actually a lot of bit flags
        # a lot of them don't seem to be relevant for spectroscopy
        eval_info_mask = struct.unpack("Q", fin.read(8))[0]
        acq_end = eval_info_mask & 1
        rt_feedback = eval_info_mask >> 1 & 1
        hp_feedback = eval_info_mask >> 2 & 1
        sync_data = eval_info_mask >> 5 & 1
        raw_data_correction = eval_info_mask >> 10 & 1
        ref_phase_stab_scan = eval_info_mask >> 14 & 1
        phase_stab_scan = eval_info_mask >> 15 & 1
        sign_rev = eval_info_mask >> 17 & 1
        phase_correction = eval_info_mask >> 21 & 1
        pat_ref_scan = eval_info_mask >> 22 & 1
        pat_ref_ima_scan = eval_info_mask >> 23 & 1
        reflect = eval_info_mask >> 24 & 1
        noise_adj_scan = eval_info_mask >> 25 & 1

        if acq_end:
            break

        # if any of these flags are set then we should ignore the scan data
        if rt_feedback or hp_feedback or phase_correction or noise_adj_scan or sync_data:
            fin.seek(start_position + DMA_length)
            continue

        # now come the actual parameters of the scan
        num_samples, num_channels = struct.unpack("HH", fin.read(4))
        builder.set_num_channels(num_channels)

        # the loop counters are a set of 14 shorts which are used as indices
        # for the parameters an acquisition might loop over, including
        # averaging repetitions, COSY echo time increments and CSI phase
        # encoding steps
        # we have no prior knowledge about which counters might loop in a given
        # scan so we have to read in all scans and then sort out the data shape
        loop_counters = struct.unpack("14H", fin.read(28))

        cut_off_data, kspace_centre_column, coil_select, readout_offcentre = struct.unpack("IHHI", fin.read(12))
        time_since_rf, kspace_centre_line_num, kspace_centre_partition_num = struct.unpack("IHH", fin.read(8))

        ice_program_params = struct.unpack("4H", fin.read(8))
        free_params = struct.unpack("4H", fin.read(8))

        # there are some dummy points before the data starts
        num_dummy_points = free_params[0]

        # we want our np to be the largest power of two within the num_samples - num_dummy_points
        np = int(2 ** numpy.floor(numpy.log2(num_samples - num_dummy_points)))
        builder.set_np(np)

        slice_position = struct.unpack("7f", fin.read(28))

        # construct a numpy ndarray to hold the data from all the channels in this scan
        scan_data = numpy.zeros((num_channels, np), dtype='complex')

        # loop over all the channels and extract data
        for channel_index in range(num_channels):
            channel_id, ptab_pos_neg = struct.unpack("Hh", fin.read(4))
            raw_data = struct.unpack("<{}f".format(num_samples * 2), fin.read(num_samples * 4 * 2))
            # turn the raw data into complex pairs
            data_iter = iter(raw_data)
            complex_iter = (complex(r, -i) for r, i in zip(data_iter, data_iter))
            scan_data[channel_index, :] = numpy.fromiter(complex_iter, "complex64", num_samples)[num_dummy_points:(num_dummy_points + np)]

            # the vb format repeats all the header data for each channel in
            # turn, obviously this is redundant so we read all but the channel
            # index from the next header here
            fin.read(124)

        # pass the data from this scan to the builder
        builder.add_scan(loop_counters, scan_data)

        # go to the next scan and the top of the loop
        fin.seek(start_position + DMA_length)


def load_twix_vd(fin, builder):
    twix_id, num_measurements = struct.unpack("II", fin.read(8))
    # vd file can contain multiple measurements, but we only want the MRS
    # assume that the MRS is the last measurement
    measurement_index = num_measurements - 1

    # measurement headers are each 152 bytes at start of file
    fin.seek(8 + 152 * measurement_index)
    meas_id, file_id, offset, length, patient_name, protocol_name = struct.unpack("IIQQ64s64s", fin.read(152))
    # offset points to where the actual data is in the file
    fin.seek(offset)

    # start with the header
    header_size = struct.unpack("I", fin.read(4))[0]
    header = fin.read(header_size - 4)
    header = header.decode('latin-1')
    builder.set_header_string(header)

    # read each scan until we hit the acq_end flag
    while True:

        # read the initial position, combined with DMA_length below that
        # tells us how to get to the start of the next scan
        initial_position = fin.tell()

        # the first four bytes contain some composite information,
        # read in an int and do bit shift magic to get the values
        temp = struct.unpack("I", fin.read(4))[0]
        DMA_length = temp & (2 ** 26 - 1)
        pack_flag = (temp >> 25) & 1
        PCI_rx = temp >> 26
        meas_uid, scan_counter, time_stamp, pmu_time_stamp = struct.unpack("IIII", fin.read(16))
        system_type, ptab_pos_delay, ptab_pos_x, ptab_pos_y, ptab_pos_z, reserved = struct.unpack("HHIIII", fin.read(20))

        # more composite information
        eval_info_mask = struct.unpack("Q", fin.read(8))[0]
        acq_end = eval_info_mask & 1
        rt_feedback = eval_info_mask >> 1 & 1
        hp_feedback = eval_info_mask >> 2 & 1
        sync_data = eval_info_mask >> 5 & 1
        raw_data_correction = eval_info_mask >> 10 & 1
        ref_phase_stab_scan = eval_info_mask >> 14 & 1
        phase_stab_scan = eval_info_mask >> 15 & 1
        sign_rev = eval_info_mask >> 17 & 1
        phase_correction = eval_info_mask >> 21 & 1
        pat_ref_scan = eval_info_mask >> 22 & 1
        pat_ref_ima_scan = eval_info_mask >> 23 & 1
        reflect = eval_info_mask >> 24 & 1
        noise_adj_scan = eval_info_mask >> 25 & 1

        # if acq_end is set, there is no more data
        if acq_end:
            break

        # there are some data frames that contain auxilliary data, we ignore those for now
        if rt_feedback or hp_feedback or phase_correction or noise_adj_scan or sync_data:
            fin.seek(initial_position + DMA_length)
            continue

        num_samples, num_channels = struct.unpack("HH", fin.read(4))
        builder.set_num_channels(num_channels)
        loop_counters = struct.unpack("14H", fin.read(28))
        cut_off_data, kspace_centre_column, coil_select, readout_offcentre = struct.unpack("IHHI", fin.read(12))
        time_since_rf, kspace_centre_line_num, kspace_centre_partition_num = struct.unpack("IHH", fin.read(8))
        slice_position = struct.unpack("7f", fin.read(28))
        ice_program_params = struct.unpack("24H", fin.read(48))
        reserved_params = struct.unpack("4H", fin.read(8))
        fid_start_offset = ice_program_params[4]
        num_dummy_points = reserved_params[0]
        fid_start = fid_start_offset + num_dummy_points
        np = int(2 ** numpy.floor(numpy.log2(num_samples - fid_start)))
        builder.set_np(np)
        application_counter, application_mask, crc = struct.unpack("HHI", fin.read(8))

        # read the data for each channel in turn
        scan_data = numpy.zeros((num_channels, np), dtype='complex')
        for channel_index in range(num_channels):

            # start with the header
            dma_length, meas_uid, scan_counter, sequence_time, channel_id = struct.unpack("III4xI4xH6x", fin.read(32))

            # now the data itself, which consists of num_samples * 4 (bytes per float) * 2 (two floats per complex)
            raw_data = struct.unpack("<{}f".format(num_samples * 2), fin.read(num_samples * 4 * 2))

            # we need to massage the list of floats into a numpy array of complex numbers
            data_iter = iter(raw_data)
            complex_iter = (complex(r, -i) for r, i in zip(data_iter, data_iter))
            scan_data[channel_index, :] = numpy.fromiter(complex_iter, "complex64", num_samples)[fid_start:(fid_start + np)]

        builder.add_scan(loop_counters, scan_data)

        # move the file pointer to the start of the next scan
        fin.seek(initial_position + DMA_length)


def load_twix(filename):
    with open(filename, 'rb') as fin:

        # we can tell the type of file from the first two uints in the header
        first_uint, second_uint = struct.unpack("II", fin.read(8))

        # reset the file pointer before giving to specific function
        fin.seek(0)

        # create a TwixBuilder object for the actual loader function to use
        builder = TwixBuilder()

        if first_uint == 0 and second_uint <= 64:
            load_twix_vd(fin, builder)
        else:
            load_twix_vb(fin, builder)

    return builder.build_mrsdata()


def anonymize_twix_header(header_string):
    """
    Removes the PHI from the supplied twix header and returns the sanitized
    version. This consists of:
    1) Replacing the patient id and name with strings of lower case x
    characters.
    2) Replacing the patient birthday with 19700101
    3) Replacing the patient gender with the number 0
    4) All references to the date and time of the exam have all alphanumeric
    characters replaced by lower case x. Other characters (notably the period)
    are left unchanged.

    :param header_string: The header string to be anonymized
    :return: The anonymized version of the header.
    """
    patient_id = re.search(r"(<ParamString.\"PatientID\">  { )(\".+\")(  }\n)", header_string).group(2)
    header_string = re.sub(patient_id, "\"" + ("x" * (len(patient_id) - 2)) + "\"", header_string)

    patient_birthday = re.search(r"(<ParamString.\"PatientBirthDay\">  { )(\".+\")(  }\n)", header_string).group(2)
    header_string = re.sub(patient_birthday, "\"19700101\"", header_string)

    patient_name = re.escape(re.search(r"(<ParamString.\"PatientName\">  { )(\".+\")(  }\n)", header_string).group(2))
    # every occurrence of patient_name, replace it with a string where all the
    # characters apart from the surrounding quotations are replaced with x
    header_string = re.sub(patient_name, lambda x: re.sub(r"[^\"]", "x", x.group()), header_string)

    patient_gender_span = re.search(r"(<ParamLong.\"PatientSex\">  { )(\d+)(  }\n)", header_string).span(2)
    header_string = header_string[:patient_gender_span[0]] + "0" + header_string[patient_gender_span[1]:]

    header_string = re.sub("(Sex\">\s+\{\s*)(\d)(\s*\})",
                           lambda match: "".join((match.group(1), "0", match.group(3))),
                           header_string)

    # We need to remove information which contains the date and time of the exam
    # this is not stored in a helpful way which complicates finding it.
    # I think that this FrameOfReference parameter is the correct time, it is
    # certainly the correct date.
    # As Siemens uses date and time to refer to other scans, we need to censor
    # any string which contains the date of this exam. Also some references
    # seem to use the date with the short form of the year so we match that
    frame_of_reference = re.search(r"(<ParamString.\"FrameOfReference\">  { )(\".+\")(  }\n)", header_string).group(2)
    exam_date_time = frame_of_reference.split(".")[10]
    exam_date = exam_date_time[2:8]

    # any string which contains the exam date has all alpha-numerics replaced
    # by the character x
    header_string = re.sub(r"\"[\d\.]*{0}[\d\.]*\"".format(exam_date),
                           lambda match: re.sub(r"\w", "x", match.group()),
                           header_string)

    return header_string


def anonymize_twix_vd(fin, fout):
    pass


def anonymize_twix_vb(fin, fout):
    # first four bytes are the size of the header
    header_size = struct.unpack("I", fin.read(4))[0]

    # read the rest of the header minus the four bytes we already read
    header = fin.read(header_size - 4)
    # for some reason the last 24 bytes of the header contain some stuff that
    # is not a string, I don't know what it is
    header_string = header[:-24].decode('windows-1252')

    anonymized_header = anonymize_twix_header(header_string)

    fout.write(struct.pack("I", header_size))
    fout.write(anonymized_header.encode('windows-1252'))
    fout.write(header[-24:])
    fout.write(fin.read())


def anonymize_twix(filename, anonymized_filename):
    with open(filename, 'rb') as fin:

        # we can tell the type of file from the first two uints in the header
        first_uint, second_uint = struct.unpack("II", fin.read(8))

        # reset the file pointer before giving to specific function
        fin.seek(0)

        with open(anonymized_filename, 'wb') as fout:
            if first_uint == 0 and second_uint <= 64:
                anonymize_twix_vd(fin, fout)
            else:
                anonymize_twix_vb(fin, fout)
