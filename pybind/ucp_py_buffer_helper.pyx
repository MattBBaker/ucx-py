cdef extern from "buffer_ops.h":
    data_buf* allocate_host_buffer(int)
    data_buf* allocate_cuda_buffer(int)
    int free_host_buffer(data_buf*)
    int free_cuda_buffer(data_buf*)

cdef class buffer_region:
    cdef data_buf* buf
    cdef int is_cuda

    def __cinit__(self):
        return

    def alloc_host(self, len):
        self.buf = allocate_host_buffer(len)
        self.is_cuda = 0

    def alloc_cuda(self, len):
        self.buf = allocate_cuda_buffer(len)
        self.is_cuda = 1

    def free_host(self):
        free_host_buffer(self.buf)

    def free_cuda(self):
        free_cuda_buffer(self.buf)